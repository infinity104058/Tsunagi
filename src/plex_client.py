"""Plex library extraction via plexapi.

plexapi is synchronous — the orchestrator wraps :func:`get_anime_shows` in
``asyncio.to_thread`` so the event loop is never blocked.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from plexapi.server import PlexServer

from src.config import PlexConfig

log = logging.getLogger(__name__)

# Season 0 with this many episodes or fewer is almost always Plex
# misclassified trailers or a single pilot — not a real OVA season.
SEASON_ZERO_MIN_EPISODES = 3


@dataclass
class PlexEpisode:
    season_num: int
    episode_num: int
    air_date: date | None  # originallyAvailableAt


@dataclass
class PlexSeason:
    season_num: int
    episode_count: int
    episodes: list[PlexEpisode] = field(default_factory=list)

    @property
    def dated_episodes(self) -> list[PlexEpisode]:
        return [e for e in self.episodes if e.air_date is not None]


@dataclass
class PlexShow:
    plex_id: int  # ratingKey
    title: str
    tvdb_id: int | None  # extracted from show.guids
    seasons: list[PlexSeason] = field(default_factory=list)


def _extract_tvdb_id(show) -> int | None:
    """Extract the TVDB ID from a show's GUID list, or None if absent."""
    for guid in getattr(show, "guids", None) or []:
        gid = getattr(guid, "id", "") or ""
        if gid.startswith("tvdb://"):
            raw = gid.removeprefix("tvdb://")
            try:
                return int(raw)
            except ValueError:
                log.warning("Unparseable TVDB GUID '%s' on show '%s'", gid, show.title)
                return None
    return None


def _to_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def get_anime_shows(cfg: PlexConfig) -> list[PlexShow]:
    """Connect to Plex and extract the configured anime library.

    Season 0 is included only if it has more than SEASON_ZERO_MIN_EPISODES
    episodes. Episodes with no air date are omitted from ``episodes`` but
    still counted in ``episode_count``.
    """
    server = PlexServer(cfg.url, cfg.token)
    section = server.library.section(cfg.library_name)
    log.info("Connected to Plex; reading library section '%s'", cfg.library_name)

    shows: list[PlexShow] = []
    for show in section.all():
        tvdb_id = _extract_tvdb_id(show)
        if tvdb_id is None:
            log.warning(
                "Show '%s' has no TVDB GUID — resolver will fall back to title search",
                show.title,
            )

        seasons: list[PlexSeason] = []
        for season in show.seasons():
            season_num = season.index
            if season_num is None:
                continue
            plex_episodes = season.episodes()
            episode_count = len(plex_episodes)

            if season_num == 0 and episode_count <= SEASON_ZERO_MIN_EPISODES:
                log.info(
                    "Skipping season 0 of '%s' (%d episode(s) — likely trailers/pilot)",
                    show.title,
                    episode_count,
                )
                continue

            episodes: list[PlexEpisode] = []
            for ep in plex_episodes:
                air_date = _to_date(getattr(ep, "originallyAvailableAt", None))
                if air_date is None:
                    # No air date: still counted in episode_count, but omitted
                    # from the episode list used for date matching.
                    continue
                episodes.append(
                    PlexEpisode(
                        season_num=season_num,
                        episode_num=ep.index or 0,
                        air_date=air_date,
                    )
                )

            if not episodes and episode_count > 0:
                log.warning(
                    "'%s' season %d has zero dated episodes — date matching "
                    "unavailable, confidence will be reduced",
                    show.title,
                    season_num,
                )

            seasons.append(
                PlexSeason(
                    season_num=season_num,
                    episode_count=episode_count,
                    episodes=episodes,
                )
            )

        seasons.sort(key=lambda s: s.season_num)
        shows.append(
            PlexShow(
                plex_id=int(show.ratingKey),
                title=show.title,
                tvdb_id=tvdb_id,
                seasons=seasons,
            )
        )

    log.info("Extracted %d show(s) from Plex", len(shows))
    return shows
