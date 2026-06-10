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

import asyncio
import logging
import sys
from collections import Counter

from src import exporter, plex_applier
from src.anime_lists import AnimeLists
from src.config import Config, ConfigError, load_config
from src.database import Database
from src.jikan_client import JikanClient
from src.plex_client import PlexShow, get_anime_shows
from src.resolver import Resolver
from src.score_aggregator import ScoreAggregator

log = logging.getLogger("plex-mal-matcher")

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


async def run_once(config: Config) -> None:
    db = Database(
        config.database.path,
        score_ttl_days=config.database.score_ttl_days,
        mapping_ttl_days=config.database.mapping_ttl_days,
    )
    await db.connect()
    jikan = JikanClient(config.jikan, db)
    try:
        anime_lists = AnimeLists(config.anime_lists)
        await anime_lists.ensure_loaded()

        # plexapi is sync — keep it off the event loop. The Plex token is
        # never logged.
        shows = await asyncio.to_thread(get_anime_shows, config.plex)
        if config.plex.exclude:
            excluded_titles = {str(x).lower() for x in config.plex.exclude}
            excluded_ids = {x for x in config.plex.exclude if isinstance(x, int)}
            before = len(shows)
            shows = [
                s for s in shows
                if s.title.lower() not in excluded_titles
                and s.tvdb_id not in excluded_ids
            ]
            if before != len(shows):
                log.info("Excluded %d show(s) via plex.exclude", before - len(shows))

        resolver = Resolver(config, db, jikan, anime_lists)
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


async def main() -> None:
    _setup_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    interval = config.schedule_interval_hours
    if interval == 0:
        log.info("schedule_interval_hours=0 — running once and exiting")
        await run_once(config)
        return

    log.info("Scheduled mode: running every %d hour(s)", interval)
    while True:
        try:
            # Reload config each cycle so override/file edits apply without a
            # container restart.
            config = load_config()
            await run_once(config)
        except Exception:
            # One failed run must not kill the container.
            log.exception("Run failed — will retry at the next interval")
        await asyncio.sleep(interval * 3600)


if __name__ == "__main__":
    asyncio.run(main())
