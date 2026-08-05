"""Web UI API: state, overrides, excludes, run trigger, search."""
from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from src.runstate import RUNNING_FLAG_STALE_SECONDS


CONFIG = """
plex: {{url: "http://x:32400", token: "SECRET_TOKEN", library_name: "Anime Shows"}}
anime_lists: {{url: "http://x", local_path: "{tmp}/al.xml", refresh_days: 9999}}
jikan: {{base_url: "http://j/v4", rate_limit_per_second: 3, rate_limit_per_minute: 60, retry_attempts: 2, retry_backoff_seconds: 0.1}}
database: {{path: "{tmp}/m.db", score_ttl_days: 7, mapping_ttl_days: 30}}
output: {{path: "{tmp}/output.json"}}
overrides: {{path: "{tmp}/overrides.yaml"}}
"""

OUTPUT = {
    "generated_at": "2026-06-10T09:00:00Z", "show_count": 1, "unresolved_count": 1,
    "shows": {
        "Attack on Titan": {
            "plex_id": 1, "tvdb_id": 267440, "resolution_source": "anime-lists",
            "status": "complete",
            "seasons": {"1": {"mal_ids": [16498], "mal_titles": ["AoT"], "score": 8.54,
                              "method": "direct", "confidence": "high",
                              "episode_offset": 0, "notes": ""}},
        }},
    "unresolved": [{"title": "Avatar", "tvdb_id": 74852, "reason": "no mapping"}],
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text(CONFIG.format(tmp=tmp_path.as_posix()))
    (tmp_path / "output.json").write_text(json.dumps(OUTPUT))
    monkeypatch.setenv("CONFIG_PATH", str(tmp_path / "config.yaml"))
    import src.webui as W
    W._state.update({"config": None, "jikan": None, "db": None})
    return TestClient(W.app), tmp_path


def test_state_returns_output_and_version(client):
    c, _ = client
    d = c.get("/api/state").json()
    assert list(d["output"]["shows"]) == ["Attack on Titan"]
    assert "version" in d
    assert d["status"]["matcher_running"] is False


def test_state_never_leaks_token(client):
    """The Plex token must never appear in any API response."""
    c, _ = client
    body = c.get("/api/state").text
    assert "SECRET_TOKEN" not in body


def test_index_served(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "Tsunagi" in r.text


def test_override_save_and_read_back(client):
    c, tmp = client
    r = c.put("/api/override", json={"tvdb_id": 555, "season": 1, "mal_ids": [38000], "note": "fix"})
    assert r.status_code == 200
    from src.config import _load_overrides
    overrides, _ = _load_overrides(str(tmp / "overrides.yaml"))
    assert overrides[0].mal_ids == [38000] and overrides[0].method == "direct"


def test_override_invalid_rejected(client):
    c, _ = client
    r = c.put("/api/override", json={"tvdb_id": 555, "season": 2, "mal_ids": []})
    assert r.status_code == 422


def test_override_without_tvdb_id_rejected(client):
    """P0-1 regression: a show with no TVDB GUID used to reach the server as
    tvdb_id 0 (Number("") in the UI), save fine, and never apply."""
    c, tmp = client
    for bad in (0, -12345):
        r = c.put("/api/override", json={"tvdb_id": bad, "season": 1, "mal_ids": [1]})
        assert r.status_code == 422
    assert not (tmp / "overrides.yaml").exists()


def test_override_upsert_in_place(client):
    c, tmp = client
    c.put("/api/override", json={"tvdb_id": 555, "season": 1, "mal_ids": [1]})
    c.put("/api/override", json={"tvdb_id": 555, "season": 1, "mal_ids": [2]})
    from src.config import _load_overrides
    overrides, _ = _load_overrides(str(tmp / "overrides.yaml"))
    matching = [o for o in overrides if o.tvdb_id == 555 and o.season == 1]
    assert len(matching) == 1 and matching[0].mal_ids == [2]


def test_override_delete(client):
    c, _ = client
    c.put("/api/override", json={"tvdb_id": 555, "season": 1, "mal_ids": [1]})
    assert c.delete("/api/override", params={"tvdb_id": 555, "season": 1}).status_code == 200
    assert c.delete("/api/override", params={"tvdb_id": 555, "season": 1}).status_code == 404


def test_exclude_add_dedupe_remove(client):
    c, _ = client
    assert c.put("/api/exclude", json={"value": "One Piece"}).status_code == 200
    assert c.put("/api/exclude", json={"value": "one piece"}).json().get("already") is True
    d = c.get("/api/state").json()
    assert "One Piece" in d["excludes"]["overrides"]
    assert c.delete("/api/exclude", params={"value": "one piece"}).status_code == 200
    assert c.delete("/api/exclude", params={"value": "nope"}).status_code == 404


def test_run_trigger_and_guard(client):
    c, tmp = client
    assert c.post("/api/run").status_code == 200
    assert (tmp / ".run-now").exists()
    assert c.post("/api/run").json().get("already_pending") is True
    (tmp / ".run-now").unlink()
    (tmp / ".matcher-running").write_text("")
    assert c.post("/api/run").status_code == 409     # refuse while running


def test_stale_running_flag_ignored(client):
    """P0-2 regression: a flag orphaned by SIGKILL/OOM (mtime never refreshed
    again) must stop blocking the Run button after the staleness window."""
    c, tmp = client
    flag = tmp / ".matcher-running"
    flag.write_text("{}")
    old = time.time() - (RUNNING_FLAG_STALE_SECONDS + 60)
    os.utime(flag, (old, old))

    assert c.get("/api/state").json()["status"]["matcher_running"] is False
    assert c.post("/api/run").status_code == 200     # no manual deletion needed


def test_search_stubbed(client, monkeypatch):
    c, _ = client
    import src.webui as W

    class StubJikan:
        async def search(self, q, type="tv", limit=10):
            return [{"mal_id": 38000, "title": "Kimetsu no Yaiba", "type": "TV",
                     "episodes": 26, "score": 8.4, "aired_from": "2019-04-06"}]

    async def fake_jikan():
        return StubJikan()

    monkeypatch.setattr(W, "_jikan", fake_jikan)
    r = c.get("/api/search", params={"q": "kimetsu"})
    assert r.status_code == 200 and r.json()[0]["mal_id"] == 38000
