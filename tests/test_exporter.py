"""output.json serialisation: shows, unresolved, and the failed block."""
from __future__ import annotations

import json

from src import exporter
from src.exporter import ShowResult
from src.main import _failed_entry
from src.plex_client import PlexShow
from src.score_aggregator import SeasonScore


def _result():
    return ShowResult(
        plex_id=1, title="FMA", tvdb_id=75579, resolution_source="anime-lists",
        seasons={1: SeasonScore(1, [121], ["FMA"], 8.1, "direct", "high", 0, "")},
    )


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_failed_block_written(tmp_path):
    out = tmp_path / "output.json"
    exporter.write(
        [_result()],
        unresolved=[{"title": "A", "tvdb_id": 100, "reason": "no mapping"}],
        failed=[{"title": "B", "tvdb_id": 200, "error": "RuntimeError: Jikan request /anime/1 exhausted 5 retries"}],
        path=str(out),
    )
    data = _read(out)
    assert data["failed_count"] == 1
    assert data["failed"] == [{
        "title": "B", "tvdb_id": 200,
        "error": "RuntimeError: Jikan request /anime/1 exhausted 5 retries",
    }]
    # failed shows are not mixed into unresolved
    assert data["unresolved_count"] == 1
    assert data["unresolved"][0]["title"] == "A"


def test_failed_block_maps_sentinel_to_null(tmp_path):
    """Shows without a TVDB ID carry the negated-plex_id sentinel internally;
    output.json must expose null, same as the unresolved block."""
    out = tmp_path / "output.json"
    exporter.write([], unresolved=[], failed=[{"title": "X", "tvdb_id": -42, "error": "boom"}],
                   path=str(out))
    assert _read(out)["failed"][0]["tvdb_id"] is None


def test_empty_failed_block_still_present(tmp_path):
    """The key always exists so the webui never needs a schema probe."""
    out = tmp_path / "output.json"
    exporter.write([_result()], unresolved=[], failed=[], path=str(out))
    data = _read(out)
    assert data["failed"] == [] and data["failed_count"] == 0


def test_failed_entry_shape():
    show = PlexShow(7, "Ghost Show", None, [])
    entry = _failed_entry(show, RuntimeError("exhausted 5 retries"))
    assert entry == {
        "title": "Ghost Show",
        "tvdb_id": -7,  # no-TVDB sentinel, exporter maps it back to null
        "error": "RuntimeError: exhausted 5 retries",
    }
