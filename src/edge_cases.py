"""Edge-case handling: cour split, shared entry, episode offset, no-match.

Rules are checked in order (A → D); the first match wins. If no rule matches,
the base candidate passes through unchanged.

Also home to the date helpers shared with season_matcher (kept here to avoid
a circular import: season_matcher imports edge_cases, never the reverse).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from src.plex_client import PlexSeason

log = logging.getLogger(__name__)

COUR_SPLIT_EPISODE_RATIO = 1.4
NO_MATCH_OVERLAP_THRESHOLD = 0.30


def parse_date(value: str | None) -> date | None:
    """ISO date string → date, handling None gracefully (MAL's aired.to is
    null for currently airing shows)."""
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def entry_window(entry: dict) -> tuple[date | None, date | None]:
    """Air window of a chain entry, with the crucial nuance that a missing
    aired_to means *currently airing* only for multi-episode TV — for a
    Movie (or any single-episode entry) MAL routinely leaves aired_to null,
    and treating that as open-ended makes a 2001 movie "overlap" every
    season aired since. Those get a closed single-day window instead."""
    start = parse_date(entry.get("aired_from"))
    end = parse_date(entry.get("aired_to"))
    if end is None and start is not None:
        if entry.get("type") == "Movie" or entry.get("episodes") == 1:
            end = start
    return start, end


def date_ranges_overlap(
    a_start: date | None,
    a_end: date | None,
    b_start: date | None,
    b_end: date | None,
) -> bool:
    """True if [a_start, a_end] and [b_start, b_end] overlap. A missing end
    date is treated as today (currently airing)."""
    if a_start is None or b_start is None:
        return False
    a_end = a_end or date.today()
    b_end = b_end or date.today()
    return max(a_start, b_start) <= min(a_end, b_end)


@dataclass
class EdgeCaseResult:
    mal_ids: list[int]
    method: str  # direct | weighted_avg | shared_entry | ova_bundle | unresolved
    confidence: str  # high | medium | low
    episode_offset: int = 0
    notes: str = ""
    score: float | None = None  # precomputed only for weighted_avg (Rule A)


def handle(
    season: PlexSeason,
    candidate: dict | None,
    chain: list[dict],
    *,
    already_assigned: set[int] = frozenset(),  # type: ignore[assignment]
    episode_offset: int = 0,
    base_method: str = "direct",
    base_confidence: str = "medium",
    base_notes: str = "",
    overlap_ratio: float | None = None,
    include_movies: bool = False,
) -> EdgeCaseResult:
    """Apply rules A–D in order; return on the first match.

    ``candidate`` is the primary chain entry chosen by season_matcher (may be
    None when nothing matched). ``overlap_ratio`` is the date-overlap ratio of
    the base match where one was computed (None for direct anime-lists maps).
    """
    plex_start, plex_end = _season_date_range(season)

    # ---------------- Rule A — Cour split (1 Plex season → N MAL entries) ----
    if (
        candidate is not None
        and candidate.get("episodes")
        and plex_start is not None
        and plex_end is not None
        and season.episode_count > candidate["episodes"] * COUR_SPLIT_EPISODE_RATIO
    ):
        overlapping = [
            e
            for e in chain
            if (include_movies or e.get("type") != "Movie")
            and date_ranges_overlap(plex_start, plex_end, *entry_window(e))
        ]
        # The split is only real if more than one chain entry spans the season.
        if len(overlapping) > 1:
            # Airing entries may have a score but episodes=None; exclude both
            # unscored and episode-less entries from the weighted average.
            scored = [e for e in overlapping if e.get("score") and e.get("episodes")]
            total_eps = sum(e["episodes"] for e in scored)
            if total_eps == 0:
                # No overlapping entry has a score yet (all airing).
                weighted_score: float | None = None
            else:
                weighted_score = round(
                    sum(e["score"] * e["episodes"] for e in scored) / total_eps, 2
                )

            notes = (
                f"Cour split: {len(overlapping)} MAL entries span this season "
                f"({', '.join(str(e['mal_id']) for e in overlapping)}). "
                f"Weighted by episode count "
                f"{'+'.join(str(e['episodes']) for e in scored)}."
            )
            if len(overlapping) > len(scored):
                notes += (
                    f" {len(overlapping) - len(scored)} entry/entries unscored "
                    f"(airing), excluded from average."
                )
            log.info("Rule A (cour split) hit for season %d", season.season_num)
            return EdgeCaseResult(
                mal_ids=[e["mal_id"] for e in overlapping],
                method="weighted_avg",
                confidence="medium",
                episode_offset=episode_offset,
                notes=notes,
                score=weighted_score,
            )

    # ---------------- Rule B — Shared entry (1 MAL entry → N Plex seasons) ---
    if candidate is not None and candidate["mal_id"] in already_assigned:
        log.info(
            "Rule B (shared entry) hit for season %d (MAL %d)",
            season.season_num, candidate["mal_id"],
        )
        return EdgeCaseResult(
            mal_ids=[candidate["mal_id"]],
            method="shared_entry",
            confidence="medium",
            episode_offset=episode_offset,
            notes="TVDB divides what MAL treats as a single entry across two seasons.",
        )

    # ---------------- Rule C — Episode offset --------------------------------
    if candidate is not None and episode_offset != 0:
        # Pass through normally; the offset is informational for downstream
        # tools (episode thumbnail matching) and doesn't change the score.
        return EdgeCaseResult(
            mal_ids=[candidate["mal_id"]],
            method=base_method,
            confidence=base_confidence,
            episode_offset=episode_offset,
            notes=base_notes or f"Episode offset {episode_offset} from anime-lists.",
        )

    # ---------------- Rule D — No match ---------------------------------------
    # Note: anime-lists direct maps never reach this branch because they pass
    # overlap_ratio=None; only the date-overlap weak-match path supplies it.
    if candidate is None or (
        overlap_ratio is not None
        and overlap_ratio < NO_MATCH_OVERLAP_THRESHOLD
        and base_confidence == "low"
    ):
        if candidate is None:
            return EdgeCaseResult(
                mal_ids=[],
                method="unresolved",
                confidence="low",
                notes=base_notes or "No candidate found in chain.",
            )
        # Candidate exists but the date overlap was below the usable
        # threshold and the episode-count fallback didn't produce anything
        # better than 'low'. Keep the low-confidence result rather than
        # discarding it outright only if the episode counts roughly agree;
        # otherwise mark unresolved.
        cand_eps = candidate.get("episodes")
        if not cand_eps or not (0.5 <= season.episode_count / cand_eps <= 2.0):
            return EdgeCaseResult(
                mal_ids=[],
                method="unresolved",
                confidence="low",
                notes=(
                    f"Date overlap {overlap_ratio:.2f} below "
                    f"{NO_MATCH_OVERLAP_THRESHOLD} and episode counts disagree "
                    f"({season.episode_count} vs {cand_eps})."
                ),
            )

    # ---------------- Pass-through --------------------------------------------
    return EdgeCaseResult(
        mal_ids=[candidate["mal_id"]],
        method=base_method,
        confidence=base_confidence,
        episode_offset=episode_offset,
        notes=base_notes,
    )


def _season_date_range(season: PlexSeason) -> tuple[date | None, date | None]:
    dated = season.dated_episodes
    if not dated:
        return None, None
    dates = [e.air_date for e in dated if e.air_date is not None]
    return min(dates), max(dates)
