"""TVDB ID → MAL ID resolution.

Resolution order per season (stop at first success):
  1. overrides.yaml entry for (tvdb_id, season_num) — always wins
  2. Non-expired database cache
  3. anime-lists lookup (when the show has a TVDB ID) → season_matcher
  4. Jikan title search, validated against the first regular season's episode
     count (±30%) → chain builder → season_matcher, confidence reduced one
     level
  5. Unresolved — written to the database with a clear reason

The resolver orchestrates chain building and season matching internally (the
spec's resolution order step 3 hands anime-lists entries to season_matcher),
so main.py receives final per-season Resolutions plus the chain that the
score aggregator needs.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.anime_lists import AnimeLists, AnimeListEntry
from src.chain_builder import ChainBuilder
from src.config import Config, Override
from src.database import Database
from src.jikan_client import JikanClient
from src.plex_client import PlexSeason, PlexShow
from src.season_matcher import MatchOptions, SeasonMatch, match_seasons, reduce_confidence

log = logging.getLogger(__name__)

SEARCH_EPISODE_TOLERANCE = 0.30


@dataclass
class Resolution:
    tvdb_id: int | None
    season_num: int
    mal_ids: list[int]
    method: str  # direct | weighted_avg | shared_entry | ova_bundle | unresolved
    confidence: str  # high | medium | low
    episode_offset: int
    source: str  # anime-lists | jikan-search | override
    notes: str
    score: float | None = None  # precomputed only for weighted_avg
    from_cache: bool = False  # True → skip the DB rewrite (keeps TTL honest)


@dataclass
class ShowResolution:
    resolutions: list[Resolution] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)
    source: str = ""  # the source used for the non-cached portion


class Resolver:
    def __init__(
        self,
        config: Config,
        db: Database,
        jikan: JikanClient,
        anime_lists: AnimeLists,
    ) -> None:
        self._config = config
        self._db = db
        self._jikan = jikan
        self._anime_lists = anime_lists
        self._chain_builder = ChainBuilder(jikan)
        # (tvdb_id, season) → Override, for O(1) lookups.
        self._overrides: dict[tuple[int, int], Override] = {
            (o.tvdb_id, o.season): o for o in config.overrides
        }

    async def resolve_show(self, show: PlexShow) -> ShowResolution:
        """Resolve every season of one show. Unresolved seasons are written
        to the database and returned with method='unresolved'."""
        resolutions: list[Resolution] = []
        pre_assigned: set[int] = set()
        pending: list[PlexSeason] = []

        # ---- Steps 1 & 2: overrides, then cache --------------------------
        for season in sorted(show.seasons, key=lambda s: s.season_num):
            override = self._find_override(show, season)
            if override is not None:
                res = _resolution_from_override(show, season, override)
                resolutions.append(res)
                pre_assigned.update(res.mal_ids)
                log.info(
                    "'%s' S%d resolved from overrides.yaml → %s",
                    show.title, season.season_num, res.mal_ids or "ova_bundle",
                )
                continue

            if show.tvdb_id is not None:
                cached = await self._db.get_mapping(show.tvdb_id, season.season_num)
                if cached is not None:
                    res = _resolution_from_cache(show, cached)
                    resolutions.append(res)
                    pre_assigned.update(res.mal_ids)
                    log.info(
                        "'%s' S%d resolved from cache (source=%s) — no API calls",
                        show.title, season.season_num, cached["source"],
                    )
                    continue

            pending.append(season)

        if not pending:
            return ShowResolution(resolutions=resolutions, chain=[], source="cache")

        # ---- Step 3: anime-lists -----------------------------------------
        entries: list[AnimeListEntry] = []
        if show.tvdb_id is not None:
            entries = self._anime_lists.lookup(show.tvdb_id)

        source: str
        root_mal_id: int | None = None
        if entries:
            source = "anime-lists"
            # Root the chain at the entry for the lowest mapped TVDB season;
            # the prequel walk corrects any imprecision.
            root_mal_id = min(
                entries, key=lambda e: e.tvdb_season if e.tvdb_season >= 0 else 999
            ).mal_id
        else:
            # ---- Step 4: Jikan title search ------------------------------
            source = "jikan-search"
            root_mal_id = await self._search_root(show)
            if root_mal_id is None:
                for season in pending:
                    res = await self._mark_unresolved(
                        show,
                        season,
                        "No anime-lists mapping found; jikan search returned "
                        "no confident match (episode count mismatch or no results)",
                    )
                    resolutions.append(res)
                return ShowResolution(resolutions=resolutions, chain=[], source=source)

        # ---- Chain build + season matching --------------------------------
        chain = await self._chain_builder.build_chain(root_mal_id)
        if not chain:
            for season in pending:
                res = await self._mark_unresolved(
                    show, season, f"Chain build from MAL {root_mal_id} produced no entries"
                )
                resolutions.append(res)
            return ShowResolution(resolutions=resolutions, chain=[], source=source)

        matches = match_seasons(
            show, chain, entries, seasons=pending, already_assigned=pre_assigned,
            options=MatchOptions(
                season_zero=self._config.match.season_zero,
                include_movie_entries=self._config.match.include_movie_entries,
            ),
        )

        for match in matches:
            if match.method == "unresolved":
                res = await self._mark_unresolved(
                    show,
                    match_season := next(
                        s for s in pending if s.season_num == match.season_num
                    ),
                    match.notes or "Season matcher found no usable candidate",
                )
                resolutions.append(res)
                continue

            confidence = match.confidence
            if source == "jikan-search" and match.method != "ova_bundle":
                # Title search is the weakest entry point — reduce one level.
                confidence = reduce_confidence(confidence)

            resolutions.append(
                Resolution(
                    tvdb_id=show.tvdb_id,
                    season_num=match.season_num,
                    mal_ids=match.mal_ids,
                    method=match.method,
                    confidence=confidence,
                    episode_offset=match.episode_offset,
                    source=source,
                    notes=match.notes,
                    score=match.score,
                )
            )

        resolutions.sort(key=lambda r: r.season_num)
        return ShowResolution(resolutions=resolutions, chain=chain, source=source)

    # ------------------------------------------------------------------ #

    def _find_override(self, show: PlexShow, season: PlexSeason) -> Override | None:
        if show.tvdb_id is None:
            return None
        return self._overrides.get((show.tvdb_id, season.season_num))

    async def _search_root(self, show: PlexShow) -> int | None:
        """Step 4: title search, validated against the FIRST REGULAR season's
        episode count (lowest non-zero season_num) — not the total across all
        seasons, because the top search result is almost always the first
        season's MAL entry. The chain builder recovers the remaining seasons
        from this root."""
        results = await self._jikan.search(show.title)
        if not results:
            return None

        regular = [s for s in show.seasons if s.season_num > 0]
        if not regular:
            return None
        first_season_eps = min(regular, key=lambda s: s.season_num).episode_count
        if first_season_eps == 0:
            return None

        top = results[0]
        top_eps = top.get("episodes")
        if not top_eps:
            log.warning(
                "Jikan top result for '%s' (MAL %s) has no episode count — "
                "cannot validate, treating as no match",
                show.title, top.get("mal_id"),
            )
            return None

        deviation = abs(top_eps - first_season_eps) / first_season_eps
        if deviation > SEARCH_EPISODE_TOLERANCE:
            log.warning(
                "Jikan top result for '%s' rejected: %d episodes vs first "
                "regular season's %d (%.0f%% off, > %.0f%% tolerance)",
                show.title, top_eps, first_season_eps,
                deviation * 100, SEARCH_EPISODE_TOLERANCE * 100,
            )
            return None

        log.info(
            "'%s' resolved root via jikan search → MAL %d ('%s')",
            show.title, top["mal_id"], top.get("title"),
        )
        return int(top["mal_id"])

    async def _mark_unresolved(
        self, show: PlexShow, season: PlexSeason, reason: str
    ) -> Resolution:
        # The unresolved table's PK is (tvdb_id, season_num) NOT NULL; shows
        # without a TVDB ID use the negated Plex ratingKey as a sentinel so
        # rows stay unique. The exporter maps negatives back to null.
        sentinel = show.tvdb_id if show.tvdb_id is not None else -show.plex_id
        await self._db.upsert_unresolved(sentinel, show.title, season.season_num, reason)
        log.warning("'%s' S%d unresolved: %s", show.title, season.season_num, reason)
        return Resolution(
            tvdb_id=show.tvdb_id,
            season_num=season.season_num,
            mal_ids=[],
            method="unresolved",
            confidence="low",
            episode_offset=0,
            source="",
            notes=reason,
        )


def _resolution_from_override(
    show: PlexShow, season: PlexSeason, override: Override
) -> Resolution:
    return Resolution(
        tvdb_id=show.tvdb_id,
        season_num=season.season_num,
        mal_ids=list(override.mal_ids),
        method=override.method,
        confidence="high",
        episode_offset=0,
        source="override",
        notes=override.note,
    )


def _resolution_from_cache(show: PlexShow, cached: dict) -> Resolution:
    return Resolution(
        tvdb_id=show.tvdb_id,
        season_num=cached["season_num"],
        mal_ids=cached["mal_ids"],
        method=cached["method"],
        confidence=cached["confidence"],
        episode_offset=cached["episode_offset"],
        source=cached["source"],
        notes=cached["notes"],
        from_cache=True,
    )
