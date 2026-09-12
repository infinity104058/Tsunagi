"""Match Plex seasons to MAL chain entries.

Seasons within a single show are matched sequentially in ascending season
order — never concurrently — because Rule B (shared entry) depends on knowing
what previous seasons resolved to.

Matching logic, run in order per season:
  1. OVA season detection (season 0 with ≤ 6 episodes, or all dated episodes
     clustering > 6 months from any chain entry's air window)
  2. Anime-lists direct season map
  3. Air date overlap matching (when step 2 is unavailable or low-confidence)
  4. Hand off to edge_cases.handle()
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from src import edge_cases
from src.anime_lists import AnimeListEntry
from src.edge_cases import entry_window
from src.plex_client import PlexSeason, PlexShow

log = logging.getLogger(__name__)

OVA_SEASON_ZERO_MAX_EPISODES = 6
OVA_CLUSTER_DISTANCE_DAYS = 183  # ~6 months
# A "bundle of loose extras" is by definition short. A full-length season
# (many regularly-aired episodes) is never an OVA bundle no matter what a
# sparse or broken relation chain implies about air-date distance — this
# guards franchises whose siblings link via Side Story/Spin-off (e.g.
# A Certain Scientific Railgun), which the chain builder does not follow,
# leaving the chain empty/mismatched for those seasons.
OVA_BUNDLE_MAX_EPISODES = 6

CONFIDENCE_LEVELS = ["low", "medium", "high"]


@dataclass(frozen=True)
class MatchOptions:
    """Behavior switches sourced from config.match."""
    season_zero: str = "bundle"  # bundle | match | skip
    include_movie_entries: bool = False


def reduce_confidence(confidence: str) -> str:
    """Drop confidence one level (floor: low)."""
    idx = CONFIDENCE_LEVELS.index(confidence) if confidence in CONFIDENCE_LEVELS else 0
    return CONFIDENCE_LEVELS[max(0, idx - 1)]


@dataclass
class SeasonMatch:
    season_num: int
    mal_ids: list[int]
    method: str  # direct | weighted_avg | shared_entry | ova_bundle | unresolved
    confidence: str  # high | medium | low
    episode_offset: int = 0
    notes: str = ""
    score: float | None = None  # precomputed only for weighted_avg


def _episode_ratio_confidence(season: PlexSeason, chain_entry: dict | None) -> str:
    """high 0.85–1.15, medium 0.70–1.30, low otherwise."""
    episodes = (chain_entry or {}).get("episodes")
    if not episodes:
        # Episode count unavailable (airing entry or entry not in chain) —
        # we can't verify, so split the difference rather than punishing a
        # legitimate anime-lists map.
        return "medium"
    ratio = season.episode_count / episodes
    if 0.85 <= ratio <= 1.15:
        return "high"
    if 0.70 <= ratio <= 1.30:
        return "medium"
    return "low"


def _season_date_range(season: PlexSeason) -> tuple[date | None, date | None]:
    dated = season.dated_episodes
    if not dated:
        return None, None
    dates = [e.air_date for e in dated if e.air_date is not None]
    return min(dates), max(dates)


def _is_ova_bundle(season: PlexSeason, chain: list[dict]) -> bool:
    """Step 1 — OVA season detection.

    Season 0 with ≤ 3 episodes is dropped at extraction and never reaches
    here; season 0 with 4–6 episodes lands here and appears in the output as
    ova_bundle (visible, unscored); season 0 with 7+ episodes gets a real
    resolution attempt. This is intentional.
    """
    if season.season_num == 0 and season.episode_count <= OVA_SEASON_ZERO_MAX_EPISODES:
        return True

    # All dated episodes clustering > 6 months from every chain entry's air
    # window also marks the season as a bundle of loose extras — but only for
    # genuinely short seasons, and only when the chain is substantial enough
    # to trust its windows. Otherwise a broken/sparse chain (siblings linked
    # via Side Story/Spin-off, which we don't follow) would falsely condemn a
    # full regular season.
    dated = season.dated_episodes
    if not dated or not chain:
        return False
    if season.episode_count > OVA_BUNDLE_MAX_EPISODES:
        return False
    # Need at least as many chain entries as would plausibly cover this
    # season; a single distant entry is not enough to declare the whole
    # season "far from the chain".
    if len(chain) < 2 and season.episode_count > 3:
        return False

    windows: list[tuple[date, date]] = []
    for entry in chain:
        start, end = entry_window(entry)
        if start is None:
            continue
        windows.append((start, end or date.today()))
    if not windows:
        return False

    for ep in dated:
        ep_date = ep.air_date
        if ep_date is None:
            # dated_episodes already filters these; explicit check rather than
            # an assert because asserts vanish under `python -O`.
            continue
        min_distance = min(
            0
            if start <= ep_date <= end
            else min(abs((ep_date - start).days), abs((ep_date - end).days))
            for start, end in windows
        )
        if min_distance <= OVA_CLUSTER_DISTANCE_DAYS:
            return False  # at least one episode is near the main chain
    return True


def _direct_map(
    season: PlexSeason,
    anime_list_entries: list[AnimeListEntry],
    chain_by_id: dict[int, dict],
) -> tuple[dict | None, int, str, str] | None:
    """Step 2 — anime-lists direct season map.

    Returns (candidate_entry_or_minimal_dict, episode_offset, confidence,
    notes) or None when no entry maps this TVDB season.
    """
    for al_entry in anime_list_entries:
        if al_entry.tvdb_season != season.season_num:
            continue
        chain_entry = chain_by_id.get(al_entry.mal_id)
        confidence = _episode_ratio_confidence(season, chain_entry)
        candidate = chain_entry or {"mal_id": al_entry.mal_id, "episodes": None}
        notes = ""
        if chain_entry is None:
            notes = (
                f"anime-lists maps to MAL {al_entry.mal_id}, which is outside "
                f"the built main chain."
            )
        return candidate, al_entry.episode_offset, confidence, notes
    return None


def _date_overlap_match(
    season: PlexSeason, chain: list[dict], include_movies: bool = False
) -> tuple[dict | None, float, str, str]:
    """Step 3 — air date overlap matching.

    Returns (candidate, overlap_ratio, confidence, notes). When the season
    has no dated episodes, falls back to episode count only with confidence
    one level lower (i.e. low).
    """
    plex_start, plex_end = _season_date_range(season)

    if plex_start is None or plex_end is None:
        # Zero dated episodes — cannot match by date.
        candidate = _closest_by_episode_count(season, chain, include_movies)
        return (
            candidate,
            0.0,
            "low",
            "No dated episodes; matched by episode count only.",
        )

    best: dict | None = None
    best_ratio = -1.0
    plex_span = (plex_end - plex_start).days or 1

    for entry in chain:
        if not include_movies and entry.get("type") == "Movie":
            continue
        mal_start, mal_end = entry_window(entry)
        if mal_start is None:
            continue
        mal_end = mal_end or date.today()

        overlap_start = max(plex_start, mal_start)
        overlap_end = min(plex_end, mal_end)
        overlap_days = max(0, (overlap_end - overlap_start).days)
        overlap_ratio = overlap_days / plex_span

        if overlap_ratio > best_ratio:
            best_ratio = overlap_ratio
            best = entry

    if best is None or best_ratio <= 0:
        # Nothing in the chain overlaps the season at all — date matching is
        # impossible. Fall back to episode count.
        candidate = _closest_by_episode_count(season, chain, include_movies)
        return (
            candidate,
            0.0,
            "low",
            "No date overlap with any chain entry; matched by episode count only.",
        )

    if best_ratio <= edge_cases.NO_MATCH_OVERLAP_THRESHOLD:
        # Weak overlap: keep the best-overlap candidate rather than falling
        # back to episode count — a cour split (Rule A) looks exactly like
        # this, because no single entry covers the whole Plex season span.
        # Rule D handles the genuinely-unmatchable case downstream.
        return (
            best,
            best_ratio,
            "low",
            (
            f"Best date overlap {best_ratio:.2f} below "
            f"{edge_cases.NO_MATCH_OVERLAP_THRESHOLD}."
            )
        )

    if best_ratio > 0.85 and _episode_ratio_confidence(season, best) == "high":
        confidence = "high"
    else:
        confidence = _episode_ratio_confidence(season, best)
        if confidence == "high":
            confidence = "medium"  # high requires both signals to agree
    return best, best_ratio, confidence, ""


def _closest_by_episode_count(
    season: PlexSeason, chain: list[dict], include_movies: bool = False
) -> dict | None:
    candidates = [
        e for e in chain
        if e.get("episodes") and (include_movies or e.get("type") != "Movie")
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda e: abs(e["episodes"] - season.episode_count))


def match_seasons(
    show: PlexShow,
    chain: list[dict],
    anime_list_entries: list[AnimeListEntry],
    seasons: list[PlexSeason] | None = None,
    already_assigned: set[int] | None = None,
    options: MatchOptions = MatchOptions(),
) -> list[SeasonMatch]:
    """Match each Plex season to one or more MAL entries.

    ``seasons`` restricts matching to a subset (the resolver passes only the
    seasons not already satisfied by an override or the cache).
    ``already_assigned`` seeds Rule B with MAL IDs claimed by those
    pre-resolved seasons.
    """
    assigned: set[int] = set(already_assigned or set())
    chain_by_id = {e["mal_id"]: e for e in chain}
    to_match = sorted(seasons if seasons is not None else show.seasons,
                      key=lambda s: s.season_num)

    results: list[SeasonMatch] = []
    for season in to_match:
        if season.season_num == 0 and options.season_zero != "match":
            if options.season_zero == "skip":
                log.info("'%s' S0 skipped (match.season_zero=skip)", show.title)
                continue
            # "bundle": S0 is specials/extras in practice — never chain-match.
            results.append(SeasonMatch(
                season_num=0, mal_ids=[], method="ova_bundle", confidence="high",
                notes="Season 0 treated as specials bundle (match.season_zero=bundle).",
            ))
            continue
        match = _match_one(season, chain, chain_by_id, anime_list_entries, assigned,
                           options)
        results.append(match)
        assigned.update(match.mal_ids)
        log.log(
            logging.INFO if match.method != "unresolved" else logging.WARNING,
            "'%s' S%d → %s (%s, confidence=%s)",
            show.title, season.season_num, match.mal_ids or "—",
            match.method, match.confidence,
        )
    return results


def _match_one(
    season: PlexSeason,
    chain: list[dict],
    chain_by_id: dict[int, dict],
    anime_list_entries: list[AnimeListEntry],
    assigned: set[int],
    options: MatchOptions = MatchOptions(),
) -> SeasonMatch:
    # ---- Step 1 — OVA season detection ------------------------------------
    if _is_ova_bundle(season, chain):
        return SeasonMatch(
            season_num=season.season_num,
            mal_ids=[],
            method="ova_bundle",
            confidence="high" if season.season_num == 0 else "medium",
            notes="OVA bundle — skipped scoring.",
        )

    candidate: dict | None = None
    episode_offset = 0
    confidence = "low"
    notes = ""
    overlap_ratio: float | None = None
    method = "direct"

    # ---- Step 2 — anime-lists direct season map ---------------------------
    direct = _direct_map(season, anime_list_entries, chain_by_id)
    if direct is not None:
        candidate, episode_offset, confidence, notes = direct

    # ---- Step 3 — air date overlap (step 2 unavailable or low-confidence) --
    if direct is None or confidence == "low":
        date_candidate, ratio, date_confidence, date_notes = _date_overlap_match(
            season, chain, include_movies=options.include_movie_entries
        )
        better_than_direct = direct is None or _conf_rank(date_confidence) > _conf_rank(
            confidence
        )
        if date_candidate is not None and better_than_direct:
            candidate = date_candidate
            confidence = date_confidence
            notes = date_notes
            overlap_ratio = ratio
            episode_offset = episode_offset if direct is not None else 0

    # ---- Step 4 — edge cases -----------------------------------------------
    result = edge_cases.handle(
        season,
        candidate,
        chain,
        already_assigned=assigned,
        episode_offset=episode_offset,
        base_method=method,
        base_confidence=confidence,
        base_notes=notes,
        overlap_ratio=overlap_ratio,
        include_movies=options.include_movie_entries,
    )

    final_confidence = result.confidence
    if not season.dated_episodes and result.method in ("direct", "shared_entry"):
        # Zero dated episodes — confidence one level lower across the board.
        final_confidence = reduce_confidence(final_confidence)

    return SeasonMatch(
        season_num=season.season_num,
        mal_ids=result.mal_ids,
        method=result.method,
        confidence=final_confidence,
        episode_offset=result.episode_offset,
        notes=result.notes,
        score=result.score,
    )


def _conf_rank(confidence: str) -> int:
    return CONFIDENCE_LEVELS.index(confidence) if confidence in CONFIDENCE_LEVELS else 0
