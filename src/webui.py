"""Web UI sidecar for Tsunagi.

Runs as a second service from the same image (uvicorn src.webui:app).
Shares /data with the matcher:

  * reads  output.json           — audit view
  * writes overrides.yaml        — manual mappings + exclude list (atomic)
  * writes .run-now              — wakes the matcher's schedule loop
  * reads  .matcher-running      — run status flag

MAL search reuses JikanClient (rate limiter + retry) with its own small
cache DB (webui-cache.db) so it never contends with the matcher's SQLite.

No auth built in — put it behind Authentik like the arr stack.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from src import __version__
from src.config import ConfigError, load_config, parse_override
from src.database import Database
from src.jikan_client import JikanClient

# runstate, not main: importing src.main would drag in plexapi and the whole
# matcher graph just for a few sentinel helpers.
from src.runstate import (
    WAKE_FORCE_RESOLVE,
    matcher_running,
    request_run,
    wake_file,
)

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

app = FastAPI(title="Tsunagi webui", version=__version__, docs_url=None, redoc_url=None)

_HTML = Path(__file__).parent.parent / "webui" / "index.html"

_state: dict[str, Any] = {"config": None, "jikan": None, "db": None}


def _config():
    # Reload lazily on each request group so overrides edits made elsewhere
    # are always reflected; config parsing is cheap.
    try:
        cfg = load_config()
    except ConfigError as exc:
        raise HTTPException(500, f"Config error: {exc}") from exc
    _state["config"] = cfg
    return cfg


async def _jikan() -> JikanClient:
    if _state["jikan"] is None:
        cfg = _config()
        cache_path = str(Path(cfg.database.path).parent / "webui-cache.db")
        db = Database(cache_path,
                      score_ttl_days=cfg.database.score_ttl_days,
                      mapping_ttl_days=cfg.database.mapping_ttl_days,
                      relations_ttl_days=cfg.database.relations_ttl_days)
        await db.connect()
        _state["db"] = db
        _state["jikan"] = JikanClient(cfg.jikan, db)
    return _state["jikan"]


@app.on_event("shutdown")
async def _shutdown() -> None:
    if _state["jikan"] is not None:
        await _state["jikan"].close()
    if _state["db"] is not None:
        await _state["db"].close()


# ---------------------------------------------------------------- overrides IO

def _read_overrides_raw(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"overrides": [], "exclude": []}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    data.setdefault("overrides", [])
    data.setdefault("exclude", [])
    if data["overrides"] is None:
        data["overrides"] = []
    if data["exclude"] is None:
        data["exclude"] = []
    return data


def _write_overrides_raw(path: str, data: dict) -> None:
    header = (
        "# Managed by Tsunagi webui — hand edits are preserved but\n"
        "# comments are not. 'overrides' = manual season mappings,\n"
        "# 'exclude' = shows skipped entirely (titles or TVDB IDs).\n"
    )
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    tmp = path + ".tmp"
    Path(tmp).write_text(header + body, encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ endpoints

@app.get("/")
async def index():
    return FileResponse(_HTML)


@app.get("/api/state")
async def state():
    cfg = _config()
    out_path = Path(cfg.output.path)
    output = None
    if out_path.exists():
        import json
        try:
            output = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read output.json: %s", exc)
    ov = _read_overrides_raw(cfg.overrides_path)
    return {
        "version": __version__,
        "output": output,
        "overrides": ov["overrides"],
        "excludes": {
            "config": list(cfg.plex.exclude),
            "overrides": ov["exclude"],
        },
        "status": {
            # Staleness-aware: an orphaned flag (SIGKILL mid-run) must not
            # report as running forever.
            "matcher_running": matcher_running(cfg),
            "wake_pending": wake_file(cfg).exists(),
        },
    }


@app.get("/api/search")
async def search(q: str, type: str = "tv"):
    if not q.strip():
        return []
    if type not in ("tv", "movie", "ova", "special", "ona"):
        raise HTTPException(400, "invalid type")
    jikan = await _jikan()
    try:
        return await jikan.search(q.strip(), type=type, limit=10)
    except Exception as exc:  # rate-limit exhaustion etc. — surface cleanly
        log.exception("Jikan search failed")
        raise HTTPException(502, f"Jikan search failed: {exc}") from exc


class OverrideBody(BaseModel):
    tvdb_id: int
    season: int
    mal_ids: list[int] = Field(default_factory=list)
    method: str | None = None  # inferred when omitted; "ova_bundle" allowed
    note: str = ""


@app.put("/api/override")
async def put_override(body: OverrideBody):
    # Backstop for shows with no TVDB GUID: the resolver keys overrides on
    # tvdb_id, so anything non-positive would save "successfully" and never
    # apply (the UI's Number("") → 0 path used to hit exactly this).
    if body.tvdb_id <= 0:
        raise HTTPException(422, "overrides require a positive TVDB ID")
    cfg = _config()
    entry: dict[str, Any] = {"tvdb_id": body.tvdb_id, "season": body.season}
    if body.mal_ids:
        if len(body.mal_ids) == 1:
            entry["mal_id"] = body.mal_ids[0]
        else:
            entry["mal_ids"] = body.mal_ids
    if body.method:
        entry["method"] = body.method
    if body.note:
        entry["note"] = body.note
    try:
        parse_override(entry, 0)  # validate exactly like the matcher will
    except ConfigError as exc:
        raise HTTPException(422, str(exc)) from exc

    data = _read_overrides_raw(cfg.overrides_path)
    data["overrides"] = [
        o for o in data["overrides"]
        if not (int(o.get("tvdb_id", -1)) == body.tvdb_id
                and int(o.get("season", -999)) == body.season)
    ]
    data["overrides"].append(entry)
    _write_overrides_raw(cfg.overrides_path, data)
    log.info("Override saved: tvdb %d S%d -> %s", body.tvdb_id, body.season,
             body.mal_ids or body.method)
    return {"ok": True, "entry": entry}


@app.delete("/api/override")
async def delete_override(tvdb_id: int, season: int):
    cfg = _config()
    data = _read_overrides_raw(cfg.overrides_path)
    before = len(data["overrides"])
    data["overrides"] = [
        o for o in data["overrides"]
        if not (int(o.get("tvdb_id", -1)) == tvdb_id
                and int(o.get("season", -999)) == season)
    ]
    if len(data["overrides"]) == before:
        raise HTTPException(404, "no such override")
    _write_overrides_raw(cfg.overrides_path, data)
    return {"ok": True}


class ExcludeBody(BaseModel):
    value: str | int


@app.put("/api/exclude")
async def put_exclude(body: ExcludeBody):
    cfg = _config()
    data = _read_overrides_raw(cfg.overrides_path)
    if any(str(v).lower() == str(body.value).lower() for v in data["exclude"]):
        return {"ok": True, "already": True}
    data["exclude"].append(body.value)
    _write_overrides_raw(cfg.overrides_path, data)
    log.info("Exclude added: %r", body.value)
    return {"ok": True}


@app.delete("/api/exclude")
async def delete_exclude(value: str):
    cfg = _config()
    data = _read_overrides_raw(cfg.overrides_path)
    before = len(data["exclude"])
    data["exclude"] = [v for v in data["exclude"]
                       if str(v).lower() != value.lower()]
    if len(data["exclude"]) == before:
        raise HTTPException(404, "not in exclude list")
    _write_overrides_raw(cfg.overrides_path, data)
    return {"ok": True}


class RunBody(BaseModel):
    # force=true → re-resolve every mapping, ignoring mapping_ttl_days
    # (the webui counterpart of the --force-resolve CLI flag).
    force: bool = False


@app.post("/api/run")
async def trigger_run(body: RunBody | None = None):
    force = body.force if body is not None else False
    cfg = _config()
    if matcher_running(cfg):
        raise HTTPException(409, "matcher is already running")
    wf = wake_file(cfg)
    if wf.exists():
        # Upgrade a pending plain run to a forced one; never downgrade.
        if force and wf.read_text().strip() != WAKE_FORCE_RESOLVE:
            request_run(cfg, force_resolve=True)
            log.info("Pending run upgraded to force re-resolve")
            return {"ok": True, "already_pending": True, "force": True}
        return {"ok": True, "already_pending": True}
    request_run(cfg, force_resolve=force)
    log.info("Run trigger written%s", " (force re-resolve)" if force else "")
    return {"ok": True, "force": force}
