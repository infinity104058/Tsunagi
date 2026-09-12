"""anime_lists download resilience: a failed refresh must not abort a run
when a usable local copy exists (P0-3)."""
from __future__ import annotations

import os
import time

import httpx
import pytest

from src.anime_lists import AnimeLists
from src.config import AnimeListsConfig

MINIMAL_XML = """<anime-list>
  <anime anidbid="1" tvdbid="267440" defaulttvdbseason="1" episodeoffset="0">
    <name>Attack on Titan</name>
    <mal-id>16498</mal-id>
  </anime>
</anime-list>
"""


def _cfg(tmp_path, refresh_days=7):
    return AnimeListsConfig(
        url="http://upstream.invalid/anime-list.xml",
        local_path=str(tmp_path / "al.xml"),
        refresh_days=refresh_days,
        mal_map_url="",  # skip the Fribb JSON leg; tested via the same helper
    )


def _write_stale(tmp_path, age_days=30):
    p = tmp_path / "al.xml"
    p.write_text(MINIMAL_XML, encoding="utf-8")
    old = time.time() - age_days * 86400
    os.utime(p, (old, old))
    return p


class _FailingClient:
    """httpx.AsyncClient stand-in whose every request fails."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        raise httpx.ConnectError("egress blocked")


async def test_stale_local_copy_survives_download_failure(tmp_path, monkeypatch):
    _write_stale(tmp_path)
    monkeypatch.setattr("src.anime_lists.httpx.AsyncClient", _FailingClient)

    al = AnimeLists(_cfg(tmp_path))
    await al.ensure_loaded()  # must not raise

    entries = al.lookup(267440)
    assert entries and entries[0].mal_id == 16498


async def test_missing_local_copy_still_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("src.anime_lists.httpx.AsyncClient", _FailingClient)

    al = AnimeLists(_cfg(tmp_path))
    with pytest.raises(httpx.HTTPError):
        await al.ensure_loaded()


async def test_fresh_local_copy_skips_download(tmp_path, monkeypatch):
    p = tmp_path / "al.xml"
    p.write_text(MINIMAL_XML, encoding="utf-8")
    # Any network attempt would raise; freshness must short-circuit it.
    monkeypatch.setattr("src.anime_lists.httpx.AsyncClient", _FailingClient)

    al = AnimeLists(_cfg(tmp_path))
    await al.ensure_loaded()
    assert al.lookup(267440)
