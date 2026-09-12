"""Rating computation and Plex write logic (with fake Plex objects)."""
from __future__ import annotations

import pytest

import src.plex_applier as pa
from src.config import ApplyConfig, PlexConfig
from src.exporter import ShowResult
from src.plex_applier import RatingPlan, apply_to_plex, build_plans
from src.plex_client import PlexSeason, PlexShow
from src.score_aggregator import SeasonScore


def score(num, value, conf="high"):
    return SeasonScore(num, [1], ["T"], value, "direct", conf, 0, "")


def null_score(num, method="ova_bundle"):
    return SeasonScore(num, [], [], None, method, "high", 0, "")


@pytest.fixture
def shows():
    return [
        PlexShow(10, "Multi", 100, [PlexSeason(1, 12, []), PlexSeason(2, 24, [])]),
        PlexShow(20, "Single", 200, [PlexSeason(1, 13, [])]),
        PlexShow(30, "AllOva", 300, [PlexSeason(0, 5, [])]),
        PlexShow(40, "LowConf", 400, [PlexSeason(1, 12, []), PlexSeason(2, 12, [])]),
    ]


@pytest.fixture
def results():
    return [
        ShowResult(10, "Multi", 100, "anime-lists", {1: score(1, 8.0), 2: score(2, 9.0)}),
        ShowResult(20, "Single", 200, "anime-lists", {1: score(1, 7.77)}),
        ShowResult(30, "AllOva", 300, "anime-lists", {0: null_score(0)}),
        ShowResult(40, "LowConf", 400, "anime-lists",
                   {1: score(1, 8.0, conf="low"), 2: score(2, 6.0)}),
    ]


def test_mean_average_and_single_season(shows, results):
    plans, skipped = build_plans(results, shows, ApplyConfig(enabled=True))
    by = {p.plex_id: p for p in plans}
    assert by[10].show_rating == 8.5
    assert by[10].season_ratings == {1: 8.0, 2: 9.0}
    assert by[20].show_rating == 7.8         # single season → its own score
    assert 30 not in by                       # all-OVA show untouched
    assert skipped == 1                       # the OVA season


def test_episode_weighted_average(shows, results):
    plans, _ = build_plans(results, shows, ApplyConfig(enabled=True, show_average="episode_weighted"))
    by = {p.plex_id: p for p in plans}
    assert by[10].show_rating == round((8.0 * 12 + 9.0 * 24) / 36, 1)


def test_min_confidence_filter(shows, results):
    plans, skipped = build_plans(results, shows, ApplyConfig(enabled=True, min_confidence="medium"))
    by = {p.plex_id: p for p in plans}
    assert by[40].season_ratings == {2: 6.0}
    assert by[40].show_rating == 6.0
    assert skipped == 2                       # OVA + low-conf season


# ---------------------------------------------------------------- Plex I/O

class FakeSeason:
    def __init__(self, idx, user=None, audience=None):
        self.index, self.userRating, self.audienceRating = idx, user, audience
        self.audienceRatingImage = None
    def rate(self, r): self.userRating = r
    def editAudienceRating(self, r, locked=True): self.audienceRating = r
    def editField(self, field, value, locked=None): setattr(self, field, value)


class FakeShow:
    def __init__(self):
        self.audienceRating = None
        self.userRating = None
        self.audienceRatingImage = None
        self._seasons = [FakeSeason(1), FakeSeason(2)]
    def seasons(self): return self._seasons
    def editAudienceRating(self, r, locked=True): self.audienceRating = r
    def rate(self, r): self.userRating = r
    def editField(self, field, value, locked=None): setattr(self, field, value)


@pytest.fixture
def fake_server(monkeypatch):
    show = FakeShow()

    class FakeServer:
        def __init__(self, *a, **k): pass
        def fetchItem(self, _id): return show

    monkeypatch.setattr(pa, "PlexServer", FakeServer)
    return show


def _pc():
    return PlexConfig(url="http://x", token="t", library_name="A")


def test_split_field_writes(fake_server):
    """Shows → audienceRating, seasons → userRating (via rate())."""
    stats, written = apply_to_plex(
        [RatingPlan(10, "Multi", 8.5, {1: 8.0, 2: 9.0})], _pc(), ApplyConfig(enabled=True))
    assert stats.shows_updated == 1 and stats.seasons_updated == 2 and stats.errors == 0
    assert fake_server.audienceRating == 8.5
    assert fake_server._seasons[0].userRating == 8.0
    assert fake_server._seasons[1].userRating == 9.0
    assert set(written) == {(10, "show", -1, 8.5), (10, "season", 1, 8.0), (10, "season", 2, 9.0)}


def test_idempotent_second_run_skips(fake_server):
    plan = [RatingPlan(10, "Multi", 8.5, {1: 8.0, 2: 9.0})]
    apply_to_plex(plan, _pc(), ApplyConfig(enabled=True))
    stats, written = apply_to_plex(plan, _pc(), ApplyConfig(enabled=True))
    assert written == []
    assert stats.skipped_unchanged == 3       # show + 2 seasons already correct


def test_dry_run_writes_nothing(monkeypatch):
    show = FakeShow()

    class FakeServer:
        def __init__(self, *a, **k): pass
        def fetchItem(self, _id): return show

    monkeypatch.setattr(pa, "PlexServer", FakeServer)
    stats, written = apply_to_plex(
        [RatingPlan(10, "Multi", 8.5, {1: 8.0, 2: 9.0})], _pc(),
        ApplyConfig(enabled=True, dry_run=True))
    assert written == []
    assert show.audienceRating is None
    assert show.audienceRatingImage is None   # image fix-up respects dry_run too
    assert show._seasons[0].userRating is None
    assert stats.shows_updated == 1 and stats.seasons_updated == 2   # counted, not written


# ---------------------------------------------------- audienceRatingImage fix

def test_rating_image_set_when_absent(fake_server):
    """A written audienceRating is invisible in Plex UIs without an
    audienceRatingImage — the applier must backfill one."""
    apply_to_plex([RatingPlan(10, "Multi", 8.5, {1: 8.0})], _pc(), ApplyConfig(enabled=True))
    assert fake_server.audienceRatingImage == "imdb://image.rating"
    # Seasons default to the user field, which needs no image.
    assert fake_server._seasons[0].audienceRatingImage is None


def test_rating_image_never_overwrites_existing(fake_server):
    fake_server.audienceRatingImage = "themoviedb://image.rating"
    apply_to_plex([RatingPlan(10, "Multi", 8.5, {})], _pc(), ApplyConfig(enabled=True))
    assert fake_server.audienceRatingImage == "themoviedb://image.rating"


def test_rating_image_backfilled_even_when_rating_unchanged(fake_server):
    """Libraries rated before this fix have correct-but-invisible ratings;
    the idempotent skip must not also skip the image."""
    fake_server.audienceRating = 8.5
    stats, written = apply_to_plex(
        [RatingPlan(10, "Multi", 8.5, {})], _pc(), ApplyConfig(enabled=True))
    assert written == [] and stats.skipped_unchanged == 1
    assert fake_server.audienceRatingImage == "imdb://image.rating"


def test_rating_image_empty_disables(fake_server):
    apply_to_plex([RatingPlan(10, "Multi", 8.5, {})], _pc(),
                  ApplyConfig(enabled=True, rating_image=""))
    assert fake_server.audienceRatingImage is None


def test_rating_image_on_seasons_when_audience(fake_server):
    apply_to_plex([RatingPlan(10, "Multi", 8.5, {1: 8.0})], _pc(),
                  ApplyConfig(enabled=True, season_field="audience"))
    assert fake_server._seasons[0].audienceRatingImage == "imdb://image.rating"
