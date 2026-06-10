"""Apply MAL scores to Plex rating fields so Kometa overlays can render them.

Pipeline position: after export. Pure computation (which item gets which
rating) is separated from Plex I/O so it can be tested without a server.

Rating logic:
  * Each scored season → its own score on the Plex season item.
  * Show → average of its scored seasons ("mean" or "episode_weighted"),
    or, when exactly one season is scored, that season's score directly.
  * ova_bundle / unresolved seasons (score is None) are never rated and
    never count toward the show average.
  * Seasons below apply.min_confidence are excluded the same way.
  * Shows with no qualifying season are left completely untouched.

Writes are idempotent: the current Plex value is compared at 1-decimal
precision and matching items are skipped. plexapi's editAudienceRating /
editUserRating lock the field by default so Plex agent refreshes don't
revert the values (apply.lock_fields controls this).

NOTE: Kometa's mass_*_rating_update operations must stay OFF for this
library or Kometa will overwrite these per-season values every run.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from plexapi.server import PlexServer

from src.config import ApplyConfig, PlexConfig
from src.database import Database
from src.exporter import ShowResult
from src.plex_client import PlexShow

log = logging.getLogger(__name__)

_CONF_RANK = {"low": 0, "medium": 1, "high": 2}


@dataclass
class RatingPlan:
    """Everything the I/O layer needs, computed up front."""
    plex_id: int
    title: str
    show_rating: float | None  # None → leave the show item untouched
    season_ratings: dict[int, float] = field(default_factory=dict)


@dataclass
class ApplyStats:
    seasons_updated: int = 0
    shows_updated: int = 0
    skipped_unchanged: int = 0
    skipped_no_score: int = 0
    errors: int = 0


def build_plans(
    results: list[ShowResult],
    shows: list[PlexShow],
    cfg: ApplyConfig,
) -> tuple[list[RatingPlan], int]:
    """Compute the ratings to write. Returns (plans, skipped_no_score)."""
    episode_counts: dict[int, dict[int, int]] = {
        s.plex_id: {sea.season_num: sea.episode_count for sea in s.seasons}
        for s in shows
    }
    min_rank = _CONF_RANK[cfg.min_confidence]

    plans: list[RatingPlan] = []
    skipped = 0
    for result in results:
        season_ratings: dict[int, float] = {}
        weights: dict[int, int] = {}
        for num, season in result.seasons.items():
            if season.score is None:
                skipped += 1
                continue
            if _CONF_RANK.get(season.confidence, 0) < min_rank:
                skipped += 1
                continue
            season_ratings[num] = round(season.score, 1)
            weights[num] = episode_counts.get(result.plex_id, {}).get(num, 0)

        if not season_ratings:
            continue  # nothing qualifying — show stays untouched

        if len(season_ratings) == 1:
            show_rating = next(iter(season_ratings.values()))
        elif cfg.show_average == "episode_weighted" and sum(weights.values()) > 0:
            total = sum(weights[n] for n in season_ratings)
            show_rating = round(
                sum(season_ratings[n] * weights[n] for n in season_ratings) / total, 1
            )
        else:
            show_rating = round(
                sum(season_ratings.values()) / len(season_ratings), 1
            )

        plans.append(RatingPlan(
            plex_id=result.plex_id,
            title=result.title,
            show_rating=show_rating,
            season_ratings=season_ratings,
        ))
    return plans, skipped


def _plex_attr(field: str) -> str:
    return "userRating" if field == "user" else "audienceRating"


def _current_rating(item, plex_field: str) -> float | None:
    value = getattr(item, plex_field, None)
    return round(float(value), 1) if value is not None else None


def _write_rating(item, field: str, rating: float, locked: bool) -> None:
    if field == "user":
        item.editUserRating(rating, locked=locked)
    else:
        item.editAudienceRating(rating, locked=locked)


def apply_to_plex(
    plans: list[RatingPlan],
    plex_cfg: PlexConfig,
    cfg: ApplyConfig,
) -> tuple[ApplyStats, list[tuple[int, str, int, float]]]:
    """Synchronous Plex I/O (run via asyncio.to_thread). Returns stats and
    the list of writes performed, for the caller to record in the DB."""
    show_attr = _plex_attr(cfg.field)
    season_attr = _plex_attr(cfg.season_field)
    stats = ApplyStats()
    written: list[tuple[int, str, int, float]] = []

    server = PlexServer(plex_cfg.url, plex_cfg.token)
    prefix = "[DRY RUN] would set" if cfg.dry_run else "Set"

    for plan in plans:
        try:
            show = server.fetchItem(plan.plex_id)
        except Exception:
            log.exception("Could not fetch Plex item %d ('%s') — skipping",
                          plan.plex_id, plan.title)
            stats.errors += 1
            continue

        # ---- show level ------------------------------------------------
        if plan.show_rating is not None:
            current = _current_rating(show, show_attr)
            if current == plan.show_rating:
                stats.skipped_unchanged += 1
            else:
                log.info("%s '%s' show %s %.1f (was %s)",
                         prefix, plan.title, cfg.field, plan.show_rating,
                         f"{current:.1f}" if current is not None else "unset")
                if not cfg.dry_run:
                    try:
                        _write_rating(show, cfg.field, plan.show_rating, cfg.lock_fields)
                        written.append((plan.plex_id, "show", -1, plan.show_rating))
                        stats.shows_updated += 1
                    except Exception:
                        log.exception("Failed writing show rating for '%s'", plan.title)
                        stats.errors += 1
                else:
                    stats.shows_updated += 1

        # ---- season level ----------------------------------------------
        try:
            live_seasons = {s.index: s for s in show.seasons() if s.index is not None}
        except Exception:
            log.exception("Could not list seasons for '%s'", plan.title)
            stats.errors += 1
            continue

        for num, rating in sorted(plan.season_ratings.items()):
            season = live_seasons.get(num)
            if season is None:
                log.warning("'%s' S%d not found in Plex — skipping", plan.title, num)
                stats.errors += 1
                continue
            current = _current_rating(season, season_attr)
            if current == rating:
                stats.skipped_unchanged += 1
                continue
            log.info("%s '%s' S%d %s %.1f (was %s)",
                     prefix, plan.title, num, cfg.field, rating,
                     f"{current:.1f}" if current is not None else "unset")
            if cfg.dry_run:
                stats.seasons_updated += 1
                continue
            try:
                _write_rating(season, cfg.season_field, rating, cfg.lock_fields)
                written.append((plan.plex_id, "season", num, rating))
                stats.seasons_updated += 1
            except Exception:
                log.exception("Failed writing rating for '%s' S%d", plan.title, num)
                stats.errors += 1

    return stats, written


async def record_writes(db: Database, written: list[tuple[int, str, int, float]]) -> None:
    for plex_id, level, season_num, rating in written:
        await db.upsert_applied_rating(plex_id, level, season_num, rating)
