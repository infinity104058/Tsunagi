"""SQLite persistence: mappings, applied_ratings, TTL behavior."""
from __future__ import annotations

import pytest

from src.database import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"), score_ttl_days=7, mapping_ttl_days=30)
    await database.connect()
    yield database
    await database.close()


async def test_mapping_roundtrip(db):
    await db.upsert_mapping(267440, 4, [40028, 51535], "weighted_avg", "low", 0, "anime-lists", "note", episode_count=28)
    row = await db.get_mapping(267440, 4)
    assert row is not None
    assert row["mal_ids"] == [40028, 51535]
    assert row["method"] == "weighted_avg"
    assert row["confidence"] == "low"


async def test_mapping_absent_returns_none(db):
    assert await db.get_mapping(999, 1) is None


async def test_mapping_ttl_zero_forces_stale(tmp_path):
    """mapping_ttl_days=0 means every mapping reads as stale (re-resolve)."""
    database = Database(str(tmp_path / "ttl.db"), score_ttl_days=7, mapping_ttl_days=0)
    await database.connect()
    await database.upsert_mapping(1, 1, [100], "direct", "high", 0, "anime-lists",
                                  episode_count=12)
    # With TTL 0, a freshly written mapping is already considered expired.
    row = await database.get_mapping(1, 1)
    assert row is None
    await database.close()


async def test_relations_cache_outlives_score_ttl(tmp_path):
    """P1-4 regression: relation graphs are near-static on MAL, so a
    "relations:*" key must survive score_ttl_days expiry while search keys
    (and everything else in jikan_cache) still use the short TTL."""
    database = Database(str(tmp_path / "ttl.db"), score_ttl_days=0,
                        mapping_ttl_days=30, relations_ttl_days=90)
    await database.connect()
    await database.upsert_cached_json("relations:5114", [{"relation": "Sequel"}])
    await database.upsert_cached_json("search:tv:5:fma", [{"mal_id": 5114}])
    # score_ttl_days=0: anything on the short TTL is instantly stale.
    assert await database.get_cached_json("relations:5114") == [{"relation": "Sequel"}]
    assert await database.get_cached_json("search:tv:5:fma") is None
    await database.close()


async def test_pragmas_set_on_connect(db):
    """P1-5: WAL journaling and a busy timeout are set at connect time."""
    cur = await db.conn.execute("PRAGMA journal_mode")
    assert (await cur.fetchone())[0].lower() == "wal"
    cur = await db.conn.execute("PRAGMA busy_timeout")
    assert (await cur.fetchone())[0] == 5000


# ------------------------------------------------- episode-count invalidation

async def test_episode_count_change_invalidates_mapping(db):
    """A split-cour season gaining part 2's episodes must miss the cache
    inside its TTL, not serve the stale single-entry mapping for 30 days."""
    await db.upsert_mapping(267440, 4, [40028], "direct", "high", 0,
                            "anime-lists", episode_count=16)
    assert await db.get_mapping(267440, 4, episode_count=16) is not None
    assert await db.get_mapping(267440, 4, episode_count=28) is None


async def test_episode_count_not_passed_keeps_mapping(db):
    """Callers that don't know the current count (or don't care) still hit."""
    await db.upsert_mapping(267440, 4, [40028], "direct", "high", 0,
                            "anime-lists", episode_count=16)
    assert await db.get_mapping(267440, 4) is not None


async def test_legacy_row_without_count_only_expires_by_ttl(db):
    """Rows written before the episode_count column (NULL) must not be
    invalidated by any count — no re-resolve stampede on upgrade."""
    await db.upsert_mapping(267440, 1, [16498], "direct", "high", 0, "anime-lists",
                            episode_count=None)
    assert await db.get_mapping(267440, 1, episode_count=999) is not None


async def test_migration_adds_episode_count_to_old_schema(tmp_path):
    """connect() must upgrade a pre-column database in place."""
    import sqlite3
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE mappings (
            tvdb_id INTEGER NOT NULL, season_num INTEGER NOT NULL,
            mal_ids TEXT NOT NULL, method TEXT NOT NULL,
            confidence TEXT NOT NULL, episode_offset INTEGER DEFAULT 0,
            source TEXT NOT NULL, notes TEXT DEFAULT '',
            resolved_at TEXT NOT NULL,
            PRIMARY KEY (tvdb_id, season_num)
        );
        INSERT INTO mappings VALUES
            (100, 1, '[5114]', 'direct', 'high', 0, 'anime-lists', '',
             strftime('%Y-%m-%dT%H:%M:%SZ', 'now'));
        """
    )
    conn.commit()
    conn.close()

    database = Database(path, score_ttl_days=7, mapping_ttl_days=30)
    await database.connect()
    try:
        row = await database.get_mapping(100, 1, episode_count=64)
        assert row is not None and row["mal_ids"] == [5114]
        # And new writes can store a count.
        await database.upsert_mapping(100, 2, [1], "direct", "high", 0,
                                      "anime-lists", episode_count=12)
        assert await database.get_mapping(100, 2, episode_count=13) is None
    finally:
        await database.close()


async def test_applied_ratings_upsert(db):
    await db.upsert_applied_rating(10, "show", -1, 8.5)
    await db.upsert_applied_rating(10, "season", 2, 9.0)
    await db.upsert_applied_rating(10, "season", 2, 9.1)   # overwrite
    cur = await db.conn.execute(
        "SELECT plex_id, level, season_num, rating FROM applied_ratings ORDER BY season_num")
    rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [(10, "show", -1, 8.5), (10, "season", 2, 9.1)]
