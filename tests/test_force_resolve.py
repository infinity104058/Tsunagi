"""--force-resolve: cache bypass in the resolver and the wake-file protocol
that carries the request from the webui to the matcher's schedule loop."""
from __future__ import annotations

from types import SimpleNamespace

from src.config import load_config
from src.database import Database
from src.main import _sleep_until_next_run
from src.plex_client import PlexSeason, PlexShow
from src.resolver import Resolver
from src.runstate import WAKE_FORCE_RESOLVE, consume_wake_file, request_run
from tests.test_config import write_config


class StubAnimeLists:
    def lookup(self, tvdb_id):
        return []


class StubJikan:
    async def search(self, query, type="tv", limit=5):
        return []


async def _resolve(tmp_path, force_resolve):
    cfg = load_config(write_config(tmp_path))
    db = Database(str(tmp_path / "m.db"), score_ttl_days=7, mapping_ttl_days=30)
    await db.connect()
    try:
        await db.upsert_mapping(100, 1, [5114], "direct", "high", 0, "anime-lists")
        resolver = Resolver(cfg, db, StubJikan(), StubAnimeLists(),
                            force_resolve=force_resolve)
        show = PlexShow(1, "FMA", 100, [PlexSeason(1, 64, [])])
        result = await resolver.resolve_show(show)
        return result.resolutions[0]
    finally:
        await db.close()


async def test_cache_used_without_force(tmp_path):
    res = await _resolve(tmp_path, force_resolve=False)
    assert res.from_cache is True
    assert res.mal_ids == [5114]


async def test_force_resolve_bypasses_fresh_cache(tmp_path):
    """A fresh, in-TTL mapping must be ignored: with the stub sources finding
    nothing, the season re-resolves to unresolved instead of the cached hit."""
    res = await _resolve(tmp_path, force_resolve=True)
    assert res.from_cache is False
    assert res.method == "unresolved"


async def test_force_resolve_does_not_bypass_overrides(tmp_path):
    """Overrides always win — force affects only the cache layer."""
    ov = tmp_path / "ov.yaml"
    ov.write_text("overrides:\n  - {tvdb_id: 100, season: 1, mal_id: 121}\n")
    cfg = load_config(write_config(tmp_path))
    db = Database(str(tmp_path / "m.db"), score_ttl_days=7, mapping_ttl_days=30)
    await db.connect()
    try:
        resolver = Resolver(cfg, db, StubJikan(), StubAnimeLists(),
                            force_resolve=True)
        result = await resolver.resolve_show(
            PlexShow(1, "FMA", 100, [PlexSeason(1, 64, [])]))
        assert result.resolutions[0].source == "override"
        assert result.resolutions[0].mal_ids == [121]
    finally:
        await db.close()


# ------------------------------------------------------------ wake protocol

def _rs_config(tmp_path):
    return SimpleNamespace(output=SimpleNamespace(path=str(tmp_path / "o.json")))


def test_wake_file_payload_round_trip(tmp_path):
    cfg = _rs_config(tmp_path)
    assert consume_wake_file(cfg) is None      # nothing pending

    request_run(cfg)
    assert consume_wake_file(cfg) == ""        # plain run
    assert consume_wake_file(cfg) is None      # consumed

    request_run(cfg, force_resolve=True)
    assert consume_wake_file(cfg) == WAKE_FORCE_RESOLVE


async def test_sleep_loop_reports_force(tmp_path):
    cfg = _rs_config(tmp_path)
    request_run(cfg, force_resolve=True)
    assert await _sleep_until_next_run(cfg, 300) is True

    request_run(cfg)
    assert await _sleep_until_next_run(cfg, 300) is False
