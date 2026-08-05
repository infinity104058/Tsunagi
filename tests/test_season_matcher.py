"""Season matching: the core resolution logic."""
from __future__ import annotations

from src.plex_client import PlexSeason, PlexShow
from src.season_matcher import MatchOptions, _is_ova_bundle, match_seasons

from tests.conftest import chain_entry, make_episodes


# ---------------------------------------------------------------- cour split

def test_cour_split_weighted_average(aot_show, aot_chain, anilist_entry):
    matches = {m.season_num: m for m in match_seasons(aot_show, aot_chain, [anilist_entry])}
    s4 = matches[4]
    assert s4.method == "weighted_avg"
    assert set(s4.mal_ids) == {40028, 51535}
    # (8.79*16 + 8.62*12) / 28
    assert s4.score == round((8.79 * 16 + 8.62 * 12) / 28, 2)


def test_direct_match_high_confidence(aot_show, aot_chain, anilist_entry):
    matches = {m.season_num: m for m in match_seasons(aot_show, aot_chain, [anilist_entry])}
    assert matches[1].method == "direct"
    assert matches[1].mal_ids == [16498]
    assert matches[1].confidence == "high"


# ------------------------------------------------------------- shared entry

def test_shared_entry_across_two_seasons():
    show = PlexShow(1, "Shared", 111, [
        PlexSeason(1, 13, make_episodes(1, 13, "2013-04-07")),
        PlexSeason(2, 12, make_episodes(2, 12, "2013-07-07")),
    ])
    chain = [chain_entry(500, "One Entry", 25, "2013-04-07", "2013-09-29")]
    matches = match_seasons(show, chain, [])
    assert matches[1].method == "shared_entry"
    assert matches[1].confidence == "medium"


# ----------------------------------------------------------- movie handling

def test_movie_with_null_aired_to_does_not_bleed():
    """A movie MAL entry with no end date must not overlap every later season."""
    chain = [
        chain_entry(38000, "KnY S1", 26, "2019-04-06", "2019-09-28", score=8.40),
        chain_entry(40456, "Mugen Train Movie", 1, "2020-10-16", None,
                    type="Movie", score=8.55),
        chain_entry(49926, "Mugen Train TV", 7, "2021-10-10", "2021-11-21", score=8.27),
    ]
    show = PlexShow(1, "Demon Slayer", 348545, [
        PlexSeason(1, 26, make_episodes(1, 26, "2019-04-06")),
        PlexSeason(2, 7, make_episodes(2, 7, "2021-10-10")),
    ])
    matches = {m.season_num: m for m in match_seasons(show, chain, [])}
    assert matches[2].mal_ids == [49926]
    assert 40456 not in matches[2].mal_ids


def test_include_movie_entries_opt_in():
    chain = [chain_entry(40456, "Movie", 1, "2020-10-16", None, type="Movie")]
    show = PlexShow(1, "MovieSeason", 999, [PlexSeason(5, 1, make_episodes(5, 1, "2020-10-16"))])
    matches = match_seasons(show, chain, [], options=MatchOptions(include_movie_entries=True))
    assert matches[0].mal_ids == [40456]


# ------------------------------------------------------------ OVA detection

def test_ova_bundle_short_far_cluster():
    """Genuine loose OVA extras far from the chain → bundle."""
    ova = PlexSeason(0, 4, make_episodes(0, 4, "2018-01-01", step_days=30))
    chain = [chain_entry(1, "Main", 24, "2010-01-01", "2010-06-01")]
    assert _is_ova_bundle(ova, chain) is True


def test_full_season_never_ova_even_with_broken_chain():
    """A 24-ep season must never be flagged OVA (the Railgun bug)."""
    railgun = PlexSeason(1, 24, make_episodes(1, 24, "2013-04-12"))
    broken_chain = [chain_entry(8937, "Index II", 24, "2010-10-08", "2011-04-01")]
    assert _is_ova_bundle(railgun, broken_chain) is False
    assert _is_ova_bundle(railgun, []) is False


def test_season_zero_small_is_bundle():
    s0 = PlexSeason(0, 5, [])
    chain = [chain_entry(1, "Main", 24, "2010-01-01", "2010-06-01")]
    assert _is_ova_bundle(s0, chain) is True


# ------------------------------------------------------- season-zero policy

def test_season_zero_policy_bundle_default():
    show = PlexShow(1, "Haikyuu", 278157, [PlexSeason(0, 13, make_episodes(0, 13, "2015-01-01"))])
    chain = [chain_entry(28891, "S2", 25, "2015-10-04", "2016-03-27")]
    matches = match_seasons(show, chain, [])
    assert matches[0].method == "ova_bundle"
    assert matches[0].mal_ids == []


def test_season_zero_policy_skip():
    show = PlexShow(1, "Haikyuu", 278157, [PlexSeason(0, 13, make_episodes(0, 13, "2015-01-01"))])
    chain = [chain_entry(28891, "S2", 25, "2015-10-04", "2016-03-27")]
    matches = match_seasons(show, chain, [], options=MatchOptions(season_zero="skip"))
    assert matches == []
