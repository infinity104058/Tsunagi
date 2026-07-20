"""Rate-limited async Jikan v4 wrapper.
 
Jikan enforces two limits: 3 requests/second AND 60 requests/minute. A
sustained 3 req/s burst hits the per-minute cap in 20 seconds, so both must be
enforced. This client uses a dual-window token bucket: an asyncio.Lock-
protected deque of request timestamps; before each request it evicts
timestamps older than 60s, then sleeps until both windows (≤ N in the last 1s,
≤ M in the last 60s) have room.
 
Every method checks the database cache before any network call and writes back
after:
  - get_series   → mal_entries (score_ttl_days)
  - get_relations → jikan_cache key "relations:{mal_id}"
  - search        → jikan_cache key "search:{type}:{query.lower()}"
"""
from __future__ import annotations
 
import asyncio
import logging
import time
from collections import deque
from typing import Any
 
import httpx
 
from src.config import JikanConfig
from src.database import Database
 
log = logging.getLogger(__name__)
 
# 429/503 are rate limiting; 500/502/504 are Jikan's own upstream (MAL
# scrape) failing, which is routine on the public instance and usually
# resolves within one retry.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
 
 
def _date_only(iso_ts: str | None) -> str | None:
    """Jikan returns full ISO timestamps for aired dates; keep the date part."""
    if not iso_ts:
        return None
    return iso_ts[:10]
 
 
def normalise_series(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten a Jikan anime object into the shape used throughout the app
    (and stored in mal_entries)."""
    aired = data.get("aired") or {}
    return {
        "mal_id": data["mal_id"],
        "title": data.get("title"),
        "title_english": data.get("title_english"),
        "type": data.get("type"),
        "episodes": data.get("episodes"),
        "score": data.get("score"),
        "members": data.get("members"),
        "aired_from": _date_only(aired.get("from")),
        "aired_to": _date_only(aired.get("to")),
    }
 
 
class _DualWindowRateLimiter:
    """Enforces both a per-second and a per-minute request budget."""
 
    def __init__(self, per_second: int, per_minute: int) -> None:
        self._per_second = per_second
        self._per_minute = per_minute
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()
 
    async def acquire(self) -> None:
        while True:
            wait: float
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 60.0:
                    self._timestamps.popleft()
 
                in_last_second = [t for t in self._timestamps if now - t < 1.0]
 
                if (
                    len(in_last_second) < self._per_second
                    and len(self._timestamps) < self._per_minute
                ):
                    self._timestamps.append(now)
                    return
 
                if len(self._timestamps) >= self._per_minute:
                    # Minute window full: wait until the oldest entry ages out.
                    wait = 60.0 - (now - self._timestamps[0]) + 0.02
                else:
                    # Second window full: wait until the oldest of the last
                    # second ages out.
                    wait = 1.0 - (now - in_last_second[0]) + 0.02
            await asyncio.sleep(max(wait, 0.02))
 
 
class JikanClient:
    def __init__(self, cfg: JikanConfig, db: Database) -> None:
        self._cfg = cfg
        self._db = db
        self._limiter = _DualWindowRateLimiter(
            cfg.rate_limit_per_second, cfg.rate_limit_per_minute
        )
        self._client = httpx.AsyncClient(
            base_url=cfg.base_url, timeout=30, follow_redirects=True
        )
 
    async def close(self) -> None:
        await self._client.aclose()
 
    # ------------------------------------------------------------------ #
 
    async def _request(self, path: str, params: dict | None = None) -> dict | None:
        """Rate-limited GET with retry on transient statuses (RETRYABLE_STATUS).
 
        Returns the parsed JSON body, or None on 404. Raises after retries
        are exhausted on retryable statuses, or immediately on the rest.
        """
        last_exc: Exception | None = None
        for attempt in range(1, self._cfg.retry_attempts + 1):
            await self._limiter.acquire()
            try:
                resp = await self._client.get(path, params=params)
            except httpx.HTTPError as exc:
                # Network-level failure: treat like a retryable response.
                last_exc = exc
                backoff = self._cfg.retry_backoff_seconds * attempt
                log.warning(
                    "Jikan request %s failed (%s) — retry %d/%d in %.1fs",
                    path, exc.__class__.__name__, attempt,
                    self._cfg.retry_attempts, backoff,
                )
                await asyncio.sleep(backoff)
                continue
 
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code in RETRYABLE_STATUS:
                backoff = self._cfg.retry_backoff_seconds * attempt
                log.warning(
                    "Jikan returned %d for %s — retry %d/%d in %.1fs",
                    resp.status_code, path, attempt,
                    self._cfg.retry_attempts, backoff,
                )
                await asyncio.sleep(backoff)
                continue
            # Any other non-200: raise immediately.
            resp.raise_for_status()
 
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(
            f"Jikan request {path} exhausted {self._cfg.retry_attempts} retries"
        )
 
    # ------------------------------------------------------------------ #
 
    async def get_series(self, mal_id: int) -> dict | None:
        """GET /anime/{mal_id} — normalised series dict, or None on 404.
        Cached in mal_entries."""
        cached = await self._db.get_mal_entry(mal_id)
        if cached is not None:
            return cached
 
        body = await self._request(f"/anime/{mal_id}")
        if body is None:
            log.warning("Jikan: MAL ID %d not found (404)", mal_id)
            return None
        entry = normalise_series(body["data"])
        await self._db.upsert_mal_entry(entry)
        return entry
 
    async def get_relations(self, mal_id: int) -> list[dict]:
        """GET /anime/{mal_id}/relations — list of relation objects.
        Each: { relation: str, entry: [{ mal_id, name, type, url }] }.
        Cached in jikan_cache under "relations:{mal_id}"."""
        cache_key = f"relations:{mal_id}"
        cached = await self._db.get_cached_json(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
 
        body = await self._request(f"/anime/{mal_id}/relations")
        relations = body["data"] if body else []
        await self._db.upsert_cached_json(cache_key, relations)
        return relations
 
    async def search(self, query: str, type: str = "tv", limit: int = 5) -> list[dict]:
        """GET /anime?q={query}&type={type} — normalised series list.
        Cached in jikan_cache under "search:{type}:{limit}:{query.lower()}"."""
        cache_key = f"search:{type}:{limit}:{query.lower()}"
        cached = await self._db.get_cached_json(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]
 
        body = await self._request(
            "/anime", params={"q": query, "type": type, "limit": limit}
        )
        results = [normalise_series(item) for item in (body["data"] if body else [])]
        await self._db.upsert_cached_json(cache_key, results)
        return results