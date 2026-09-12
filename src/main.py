"""Entrypoint and async orchestrator.

Per run:
  1. Load config and overrides
  2. Initialise database (create tables if not exist)
  3. Download / refresh anime-lists XML if stale
  4. Fetch Plex library via plex_client.get_anime_shows()
  5. Per show (concurrently, capped at 5 — the Jikan client's own rate
     limiter handles the per-request limit within that):
       a. resolver.resolve_show()  — internally builds the MAL chain and
          runs season matching; seasons within a show are always matched
          sequentially in ascending order (Rule B depends on it)
       b. score_aggregator.aggregate() for each matched season
       c. Write mappings to the database
  6. exporter.write() → output.json
  7. Log summary: shows processed, seasons resolved, confidence breakdown,
     unresolved count

Schedule loop: schedule_interval_hours == 0 → run once and exit; otherwise
loop forever with asyncio.sleep between runs, catching and logging exceptions
per run so one failed run does not kill the container.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from collections import Counter

from src import __version__, exporter, plex_applier
from src.anime_lists import AnimeLists
from src.config import Config, ConfigError, load_config
from src.database import Database
from src.jikan_client import JikanClient
from src.plex_client import PlexShow, get_anime_shows
from src.resolver import Resolver
from src.runstate import (
    WAKE_FORCE_RESOLVE,
    clear_running_flag,
    consume_wake_file,
    heartbeat,
    write_running_flag,
)
from src.score_aggregator import ScoreAggregator

log = logging.getLogger("tsunagi")

SHOW_CONCURRENCY = 5


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


async def _process_show(
    show: PlexShow,
    resolver: Resolver,
    aggregator: ScoreAggregator,
    db: Database,
    semaphore: asyncio.Semaphore,
    run_unresolved: list[dict],
) -> exporter.ShowResult | None:
    """Resolve, aggregate and persist one show. Returns None when nothing
    resolved (the show then only appears in the unresolved block)."""
    async with semaphore:
        result = await resolver.resolve_show(show)

        seasons: dict[int, object] = {}
        unresolved_count = 0

        for resolution in result.resolutions:
            if resolution.method == "unresolved":
                unresolved_count += 1
                run_unresolved.append(
                    {
                        "title": show.title,
                        "tvdb_id": show.tvdb_id if show.tvdb_id is not None else -show.plex_id,
                        "season_num": resolution.season_num,
                        "reason": resolution.notes,
                    }
                )
                continue

            score = await aggregator.aggregate(resolution, result.chain)
            seasons[resolution.season_num] = score

            # Persist the mapping — but never rewrite cache hits, or the
            # resolved_at refresh would make the TTL meaningless.
            if show.tvdb_id is not None and not resolution.from_cache:
                await db.upsert_mapping(
                    tvdb_id=show.tvdb_id,
                    season_num=resolution.season_num,
                    mal_ids=resolution.mal_ids,
                    method=resolution.method,
                    confidence=resolution.confidence,
                    episode_offset=resolution.episode_offset,
                    source=resolution.source,
                    notes=resolution.notes,
                )
            if show.tvdb_id is not None:
                # Clear any stale unresolved row from a previous run.
                await db.delete_unresolved(show.tvdb_id, resolution.season_num)

        if not seasons:
            return None

        status = "complete" if unresolved_count == 0 else "partial"
        return exporter.ShowResult(
            plex_id=show.plex_id,
            title=show.title,
            tvdb_id=show.tvdb_id,
            resolution_source=result.source,
            seasons=seasons,  # type: ignore[arg-type]
            status=status,
        )


async def run_once(config: Config, force_resolve: bool = False) -> None:
    # Self-expiring run flag: the heartbeat keeps the mtime fresh so the webui
    # can distinguish a live run from a flag orphaned by SIGKILL/OOM.
    flag = write_running_flag(config)
    heartbeat_task = asyncio.create_task(heartbeat(flag))
    try:
        db = Database(
            config.database.path,
            score_ttl_days=config.database.score_ttl_days,
            mapping_ttl_days=config.database.mapping_ttl_days,
            relations_ttl_days=config.database.relations_ttl_days,
        )
        await db.connect()
        jikan = JikanClient(config.jikan, db)
        try:
            anime_lists = AnimeLists(config.anime_lists)
            await anime_lists.ensure_loaded()

            # plexapi is sync — keep it off the event loop. The Plex token is
            # never logged.
            shows = await asyncio.to_thread(get_anime_shows, config.plex)
            all_excludes = tuple(config.plex.exclude) + tuple(config.override_excludes)
            if all_excludes:
                excluded_titles = {str(x).lower() for x in all_excludes}
                excluded_ids = {x for x in all_excludes if isinstance(x, int)}
                before = len(shows)
                shows = [
                    s for s in shows
                    if s.title.lower() not in excluded_titles
                    and s.tvdb_id not in excluded_ids
                ]
                if before != len(shows):
                    log.info("Excluded %d show(s) via exclude lists", before - len(shows))

            if force_resolve:
                log.info("Force re-resolve: ignoring cached mappings this run "
                         "(overrides still apply)")
            resolver = Resolver(config, db, jikan, anime_lists,
                                force_resolve=force_resolve)
            aggregator = ScoreAggregator(jikan)
            semaphore = asyncio.Semaphore(SHOW_CONCURRENCY)
            run_unresolved: list[dict] = []

            async def safe_process(show: PlexShow) -> exporter.ShowResult | None:
                try:
                    return await _process_show(
                        show, resolver, aggregator, db, semaphore, run_unresolved
                    )
                except Exception:
                    log.exception("Unhandled error while processing '%s' — skipping", show.title)
                    run_unresolved.append(
                        {
                            "title": show.title,
                            "tvdb_id": show.tvdb_id if show.tvdb_id is not None else -show.plex_id,
                            "season_num": None,
                            "reason": "Unhandled error during processing (see logs)",
                        }
                    )
                    return None

            raw_results = await asyncio.gather(*(safe_process(show) for show in shows))
            results = [r for r in raw_results if r is not None]

            exporter.write(results, run_unresolved, config.output.path)

            # ---- Apply stage: write scores into Plex for Kometa overlays --------
            if config.apply.enabled:
                plans, no_score = plex_applier.build_plans(results, shows, config.apply)
                stats, written = await asyncio.to_thread(
                    plex_applier.apply_to_plex, plans, config.plex, config.apply
                )
                await plex_applier.record_writes(db, written)
                log.info(
                    "Apply%s: %d show(s) updated, %d season(s) updated, "
                    "%d unchanged, %d skipped (no score / below min_confidence), "
                    "%d error(s)",
                    " (dry run)" if config.apply.dry_run else "",
                    stats.shows_updated, stats.seasons_updated,
                    stats.skipped_unchanged, no_score, stats.errors,
                )

            # ---- Summary -------------------------------------------------------
            confidence_counts: Counter[str] = Counter()
            method_counts: Counter[str] = Counter()
            seasons_resolved = 0
            for result in results:
                for score in result.seasons.values():
                    seasons_resolved += 1
                    confidence_counts[score.confidence] += 1
                    method_counts[score.method] += 1

            log.info(
                "Run complete: %d show(s) processed, %d in output, %d season(s) "
                "resolved (high=%d medium=%d low=%d; %s), %d unresolved",
                len(shows),
                len(results),
                seasons_resolved,
                confidence_counts.get("high", 0),
                confidence_counts.get("medium", 0),
                confidence_counts.get("low", 0),
                ", ".join(f"{m}={c}" for m, c in sorted(method_counts.items())) or "none",
                len(run_unresolved),
            )
        finally:
            await jikan.close()
            await db.close()


    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        clear_running_flag(config)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m src.main")
    parser.add_argument(
        "--force-resolve",
        action="store_true",
        help="re-resolve every mapping on the next run, ignoring "
             "mapping_ttl_days (overrides still win; the cache is rebuilt, "
             "not deleted)",
    )
    return parser.parse_args()


async def main() -> None:
    _setup_logging()
    args = _parse_args()
    log.info("Tsunagi v%s starting", __version__)
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        # docker-compose restarts us on failure; sleep first so a config typo
        # produces a slow, readable loop instead of an endless crash scroll.
        await asyncio.sleep(60)
        sys.exit(1)

    if config.schedule_interval_hours == 0:
        log.info("schedule_interval_hours=0 — running once and exiting")
        await run_once(config, force_resolve=args.force_resolve)
        return

    interval = config.schedule_interval_hours
    log.info("Scheduled mode: running every %d hour(s)", interval)
    # The CLI flag forces the first run only; later runs force when the webui
    # requests it through the wake file.
    force = args.force_resolve
    while True:
        try:
            # Reload config each cycle so override/file edits apply without a
            # container restart — including the interval itself.
            config = load_config()
            if config.schedule_interval_hours != interval:
                interval = config.schedule_interval_hours
                log.info("Schedule interval changed to %d hour(s)", interval)
            await run_once(config, force_resolve=force)
        except Exception:
            # One failed run must not kill the container.
            log.exception("Run failed — will retry at the next interval")
        if interval == 0:
            log.info("schedule_interval_hours is now 0 — exiting after this run")
            return
        force = await _sleep_until_next_run(config, interval * 3600)


async def _sleep_until_next_run(config: Config, total_seconds: float) -> bool:
    """Sleep in short slices, waking early if the webui drops a .run-now file.
    Returns True when the wake request asked for a forced re-resolve."""
    slept = 0.0
    slice_s = 15.0
    while slept < total_seconds:
        payload = consume_wake_file(config)
        if payload is not None:
            force = payload == WAKE_FORCE_RESOLVE
            log.info("Wake file detected — starting run immediately%s",
                     " (force re-resolve)" if force else "")
            return force
        await asyncio.sleep(min(slice_s, total_seconds - slept))
        slept += slice_s
    return False


if __name__ == "__main__":
    asyncio.run(main())
