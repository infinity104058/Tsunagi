"""Shared test fixtures and helpers."""
from __future__ import annotations

from datetime import date

import pytest

from src.anime_lists import AnimeListEntry
from src.plex_client import PlexEpisode, PlexSeason, PlexShow


def make_episodes(season: int, n: int, start_iso: str, step_days: int = 7):
    """n episodes for a season, one every `step_days` from start_iso."""
    start = date.fromisoformat(start_iso)
    return [
        PlexEpisode(season, i + 1, date.fromordinal(start.toordinal() + i * step_days))
        for i in range(n)
    ]


def chain_entry(mal_id, title, episodes, aired_from, aired_to,
                type="TV", score=8.0, members=100_000):
    """A normalised Jikan-style chain entry dict."""
    return {
        "mal_id": mal_id, "title": title, "type": type, "episodes": episodes,
        "score": score, "members": members,
        "aired_from": aired_from, "aired_to": aired_to,
    }


@pytest.fixture
def aot_chain():
    """Attack on Titan: S1, Final Part 1, Final Part 2 (a real cour split)."""
    return [
        chain_entry(16498, "Shingeki no Kyojin", 25, "2013-04-07", "2013-09-29",
                    score=8.54, members=4_000_000),
        chain_entry(40028, "Final Season", 16, "2020-12-07", "2021-03-29",
                    score=8.79, members=2_200_000),
        chain_entry(51535, "Final Season Part 2", 12, "2022-01-10", "2022-04-04",
                    score=8.62, members=1_300_000),
    ]


@pytest.fixture
def aot_show():
    return PlexShow(1, "Attack on Titan", 267440, [
        PlexSeason(1, 25, make_episodes(1, 25, "2013-04-07")),
        PlexSeason(4, 28, make_episodes(4, 16, "2020-12-07")
                   + make_episodes(4, 12, "2022-01-10")),
    ])


@pytest.fixture
def anilist_entry():
    return AnimeListEntry(
        anidb_id=1, tvdb_id=267440, tvdb_season=1, episode_offset=0,
        mal_id=16498, name="Attack on Titan",
    )
