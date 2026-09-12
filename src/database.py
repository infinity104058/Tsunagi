"""Async SQLite layer: schema, queries and TTL logic.

Raw SQL only — no ORM. All timestamps are timezone-aware UTC, serialised as
ISO 8601 with a ``Z`` suffix.

``mal_entries`` caches series metadata only. Relations and search responses do
not fit that schema — they are cached in ``jikan_cache`` instead. Search
results use ``score_ttl_days``; relation graphs use the much longer
``relations_ttl_days``, because MAL prequel/sequel topology is essentially
static while scores change weekly, and relation walking is the most
Jikan-intensive step of a run.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mappings (
    tvdb_id       INTEGER NOT NULL,
    season_num    INTEGER NOT NULL,
    mal_ids       TEXT NOT NULL,      -- JSON array of ints
    method        TEXT NOT NULL,      -- direct | weighted_avg | shared_entry | ova_bundle
    confidence    TEXT NOT NULL,      -- high | medium | low
    episode_offset INTEGER DEFAULT 0,
    source        TEXT NOT NULL,      -- anime-lists | jikan-search | override
    notes         TEXT DEFAULT '',
    resolved_at   TEXT NOT NULL,      -- ISO 8601
    episode_count INTEGER,            -- Plex episode count at resolve time;
                                      -- NULL on rows from before this column
    PRIMARY KEY (tvdb_id, season_num)
);

CREATE TABLE IF NOT EXISTS mal_entries (
    mal_id        INTEGER PRIMARY KEY,
    title         TEXT,
    title_english TEXT,
    type          TEXT,
    episodes      INTEGER,
    score         REAL,
    members       INTEGER,
    aired_from    TEXT,
    aired_to      TEXT,
    fetched_at    TEXT NOT NULL       -- ISO 8601
);

CREATE TABLE IF NOT EXISTS unresolved (
    tvdb_id       INTEGER NOT NULL,
    title         TEXT,
    season_num    INTEGER,
    reason        TEXT,
    attempted_at  TEXT NOT NULL,
    PRIMARY KEY (tvdb_id, season_num)
);

CREATE TABLE IF NOT EXISTS applied_ratings (
    plex_id     INTEGER NOT NULL,
    level       TEXT NOT NULL,        -- show | season
    season_num  INTEGER NOT NULL,     -- -1 for show-level
    rating      REAL NOT NULL,
    applied_at  TEXT NOT NULL,        -- UTC ISO 8601 Z
    PRIMARY KEY (plex_id, level, season_num)
);

CREATE TABLE IF NOT EXISTS jikan_cache (
    cache_key     TEXT PRIMARY KEY,   -- e.g. "relations:5114" or "search:tv:attack on titan"
    payload       TEXT NOT NULL,      -- raw JSON of the 'data' key
    fetched_at    TEXT NOT NULL       -- ISO 8601
);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Current UTC time as ISO 8601 with Z suffix."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> datetime:
    # Accept both '...Z' and '+00:00' offsets.
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _is_fresh(ts: str, ttl_days: int) -> bool:
    try:
        fetched = _parse_iso(ts)
    except (ValueError, AttributeError):
        return False
    return utc_now() - fetched <= timedelta(days=ttl_days)


class Database:
    """Owns the aiosqlite connection and all queries."""

    def __init__(
        self,
        path: str,
        score_ttl_days: int,
        mapping_ttl_days: int,
        relations_ttl_days: int = 90,
    ) -> None:
        self._path = path
        self._score_ttl_days = score_ttl_days
        self._mapping_ttl_days = mapping_ttl_days
        self._relations_ttl_days = relations_ttl_days
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        # WAL + busy_timeout: the matcher owns this file, but the webui's
        # sidecar cache and any future second reader must not hit "database is
        # locked" on the first overlap.
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.executescript(SCHEMA)
        await self._migrate()
        await self._db.commit()
        log.info("Database ready at %s", self._path)

    async def _migrate(self) -> None:
        """Additive migrations for databases created by older schemas —
        CREATE TABLE IF NOT EXISTS never alters an existing table."""
        async with self.conn.execute("PRAGMA table_info(mappings)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "episode_count" not in cols:
            await self.conn.execute(
                "ALTER TABLE mappings ADD COLUMN episode_count INTEGER"
            )
            log.info("Migrated mappings table: added episode_count column")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database.connect() was never called")
        return self._db

    # ------------------------------------------------------------------ #
    # mappings
    # ------------------------------------------------------------------ #

    async def get_mapping(
        self,
        tvdb_id: int,
        season_num: int,
        episode_count: int | None = None,
    ) -> dict[str, Any] | None:
        """Return the cached mapping if present, within mapping_ttl_days, and
        still describing the same season shape: when the caller passes the
        season's current Plex episode count and it differs from the count at
        resolve time, the mapping is treated as stale even inside its TTL —
        an airing split-cour gaining part 2's episodes must re-resolve now,
        not up to 30 days later. Rows from before the episode_count column
        (NULL) are only ever invalidated by the TTL."""
        async with self.conn.execute(
            "SELECT * FROM mappings WHERE tvdb_id = ? AND season_num = ?",
            (tvdb_id, season_num),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if not _is_fresh(row["resolved_at"], self._mapping_ttl_days):
            return None
        stored_count = row["episode_count"]
        if (
            episode_count is not None
            and stored_count is not None
            and stored_count != episode_count
        ):
            log.info(
                "Mapping tvdb %d S%d invalidated: episode count %d → %d",
                tvdb_id, season_num, stored_count, episode_count,
            )
            return None
        return {
            "tvdb_id": row["tvdb_id"],
            "season_num": row["season_num"],
            "mal_ids": json.loads(row["mal_ids"]),
            "method": row["method"],
            "confidence": row["confidence"],
            "episode_offset": row["episode_offset"],
            "source": row["source"],
            "notes": row["notes"],
            "resolved_at": row["resolved_at"],
        }

    async def upsert_mapping(
        self,
        tvdb_id: int,
        season_num: int,
        mal_ids: list[int],
        method: str,
        confidence: str,
        episode_offset: int,
        source: str,
        notes: str = "",
        *,
        # Keyword-only and defaultless: every writer must state the Plex
        # episode count at resolution time (None = genuinely unknown), or
        # its rows would silently never count-invalidate.
        episode_count: int | None,
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO mappings
                (tvdb_id, season_num, mal_ids, method, confidence,
                 episode_offset, source, notes, resolved_at, episode_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tvdb_id,
                season_num,
                json.dumps(mal_ids),
                method,
                confidence,
                episode_offset,
                source,
                notes,
                utc_now_iso(),
                episode_count,
            ),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # mal_entries
    # ------------------------------------------------------------------ #

    async def get_mal_entry(self, mal_id: int) -> dict[str, Any] | None:
        """Return the cached MAL entry if present and within score_ttl_days."""
        async with self.conn.execute(
            "SELECT * FROM mal_entries WHERE mal_id = ?", (mal_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        if not _is_fresh(row["fetched_at"], self._score_ttl_days):
            return None
        return {
            "mal_id": row["mal_id"],
            "title": row["title"],
            "title_english": row["title_english"],
            "type": row["type"],
            "episodes": row["episodes"],
            "score": row["score"],
            "members": row["members"],
            "aired_from": row["aired_from"],
            "aired_to": row["aired_to"],
        }

    async def upsert_mal_entry(self, entry: dict[str, Any]) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO mal_entries
                (mal_id, title, title_english, type, episodes, score,
                 members, aired_from, aired_to, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["mal_id"],
                entry.get("title"),
                entry.get("title_english"),
                entry.get("type"),
                entry.get("episodes"),
                entry.get("score"),
                entry.get("members"),
                entry.get("aired_from"),
                entry.get("aired_to"),
                utc_now_iso(),
            ),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # unresolved
    # ------------------------------------------------------------------ #

    async def get_unresolved(self) -> list[dict[str, Any]]:
        async with self.conn.execute(
            "SELECT * FROM unresolved ORDER BY attempted_at DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "tvdb_id": r["tvdb_id"],
                "title": r["title"],
                "season_num": r["season_num"],
                "reason": r["reason"],
                "attempted_at": r["attempted_at"],
            }
            for r in rows
        ]

    async def upsert_unresolved(
        self, tvdb_id: int, title: str, season_num: int, reason: str
    ) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO unresolved
                (tvdb_id, title, season_num, reason, attempted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tvdb_id, title, season_num, reason, utc_now_iso()),
        )
        await self.conn.commit()

    async def delete_unresolved(self, tvdb_id: int, season_num: int) -> None:
        """Remove a stale unresolved row once the season resolves successfully."""
        await self.conn.execute(
            "DELETE FROM unresolved WHERE tvdb_id = ? AND season_num = ?",
            (tvdb_id, season_num),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # jikan_cache
    # ------------------------------------------------------------------ #

    async def get_cached_json(self, cache_key: str) -> dict | list | None:
        """Return the parsed jikan_cache payload if within its TTL — keyed by
        prefix: "relations:*" uses relations_ttl_days, the rest score_ttl_days."""
        async with self.conn.execute(
            "SELECT payload, fetched_at FROM jikan_cache WHERE cache_key = ?",
            (cache_key,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        ttl_days = (
            self._relations_ttl_days
            if cache_key.startswith("relations:")
            else self._score_ttl_days
        )
        if not _is_fresh(row["fetched_at"], ttl_days):
            return None
        try:
            return json.loads(row["payload"])
        except json.JSONDecodeError:
            log.warning("Corrupt jikan_cache payload for key %s — ignoring", cache_key)
            return None

    async def upsert_applied_rating(
        self, plex_id: int, level: str, season_num: int, rating: float
    ) -> None:
        """Record a rating written to Plex (audit trail / future restore)."""
        await self.conn.execute(
            """
            INSERT INTO applied_ratings (plex_id, level, season_num, rating, applied_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (plex_id, level, season_num)
            DO UPDATE SET rating = excluded.rating, applied_at = excluded.applied_at
            """,
            (plex_id, level, season_num, rating, utc_now_iso()),
        )
        await self.conn.commit()

    async def upsert_cached_json(self, cache_key: str, payload: dict | list) -> None:
        await self.conn.execute(
            """
            INSERT OR REPLACE INTO jikan_cache (cache_key, payload, fetched_at)
            VALUES (?, ?, ?)
            """,
            (cache_key, json.dumps(payload), utc_now_iso()),
        )
        await self.conn.commit()
