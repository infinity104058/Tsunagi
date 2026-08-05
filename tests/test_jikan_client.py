"""Jikan client retry behavior: backoff timing, Retry-After, final attempt."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src.config import JikanConfig
from src.database import Database
from src.jikan_client import JikanClient


CFG = JikanConfig(
    base_url="http://j/v4",
    rate_limit_per_second=10,   # generous: the limiter must never sleep here,
    rate_limit_per_minute=100,  # so every recorded sleep is a retry backoff
    retry_attempts=3,
    retry_backoff_seconds=1.0,
)


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff sleeps without actually waiting."""
    calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        calls.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("src.jikan_client.asyncio.sleep", fake_sleep)
    return calls


@pytest.fixture
async def make_client(tmp_path):
    created = []

    async def _make(handler):
        db = Database(str(tmp_path / "t.db"), score_ttl_days=7, mapping_ttl_days=30)
        await db.connect()
        client = JikanClient(CFG, db)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=CFG.base_url
        )
        created.append((client, db))
        return client

    yield _make
    for client, db in created:
        await client.close()
        await db.close()


async def test_no_sleep_after_final_attempt(make_client, sleeps):
    """P2 regression: the full backoff used to be slept after the last
    attempt, right before raising."""
    client = await make_client(lambda request: httpx.Response(500))
    with pytest.raises(RuntimeError):
        await client._request("/anime/1")
    # 3 attempts → backoff between them only (2 sleeps, not 3).
    assert len(sleeps) == 2


async def test_retry_after_header_honoured(make_client, sleeps):
    responses = iter([
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, json={"data": {"mal_id": 1}}),
    ])
    client = await make_client(lambda request: next(responses))
    body = await client._request("/anime/1")
    assert body == {"data": {"mal_id": 1}}
    assert sleeps == [7.0]


async def test_backoff_scales_with_attempt_and_jitters(make_client, sleeps):
    responses = iter([
        httpx.Response(502),
        httpx.Response(502),
        httpx.Response(200, json={"data": {}}),
    ])
    client = await make_client(lambda request: next(responses))
    await client._request("/anime/1")
    assert len(sleeps) == 2
    # Linear base (backoff_seconds * attempt) plus up to 25% jitter.
    assert 1.0 <= sleeps[0] <= 1.25
    assert 2.0 <= sleeps[1] <= 2.5
