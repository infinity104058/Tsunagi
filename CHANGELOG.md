# Changelog

## v0.4.0

Renamed to **Tsunagi** (formerly `plex-mal-matcher`).

### Upgrading from v0.3.0
- The compose service/container names changed to `tsunagi` / `tsunagi-webui`.
  Redeploy with `docker compose up -d --build --remove-orphans` — without
  `--remove-orphans` the old containers keep running and the webui cannot
  bind port 8484.
- No config changes required. New optional key:
  `database.relations_ttl_days` (default 90).

### Fixed
- Web UI: shows without a TVDB ID can no longer save overrides that silently
  never apply (edit button disabled with an explanation; server rejects
  non-positive TVDB IDs).
- A run killed ungracefully (OOM, `docker compose down`) no longer leaves a
  stale `.matcher-running` flag that permanently disables the Run button —
  the flag now self-expires after 10 minutes without a heartbeat.
- A failed anime-lists/Fribb download no longer aborts the run when a usable
  (merely stale) local copy exists; it logs a warning and continues.
- `schedule_interval_hours` edits now take effect on the next cycle without a
  container restart; setting it to `0` mid-loop runs once more, then exits.
- Config errors no longer crashloop the container at full speed (60s pause
  before exit).
- Jikan retries no longer sleep after the final failed attempt, honour the
  `Retry-After` header on 429, and add jitter to the backoff.
- Season-level apply log lines showed the show-level field name.

### Changed
- MAL relation graphs are cached for `relations_ttl_days` (default 90)
  instead of `score_ttl_days` — relation topology is near-static, and this
  spares the most Jikan-intensive step of a run.
- SQLite now uses WAL journaling and a 5s busy timeout.
- Runtime dependencies are pinned exactly (`requirements.txt`); loose ranges
  moved to `requirements.in`, test tooling to `requirements-dev.txt` (no
  longer installed in the image).
- Added `.dockerignore` and a webui healthcheck.

### Internal
- Offline test suite (60+ tests, no Plex/network needed): season matching,
  edge-case rules, config parsing, applier plans, persistence, Jikan retry
  behaviour, web UI API. `pip install -r requirements-dev.txt && pytest`.
- Sentinel-file logic moved to `src/runstate.py` so the webui no longer
  imports the whole matcher graph.

## v0.3.0

First tagged release.

### Matcher
- Deterministic Plex-season → MAL-entry resolution: overrides → cache →
  anime-lists direct map → air-date/episode matching → Jikan search.
- Relation-chain builder (prequel/sequel walk, never side stories); cour-split
  and shared-entry detection; weighted-average scoring across split seasons.
- MAL IDs joined from Fribb's anime-lists JSON via AniDB ID (the ScudLee XML
  carries no MAL IDs).
- Configurable season-zero policy (`bundle` / `match` / `skip`) and movie-entry
  handling.
- Exclude list (config and overrides) by title or TVDB ID.
- Dual-window Jikan rate limiter with retry on 429/500/502/503/504.
- SQLite caching with per-table TTLs.

### Plex apply stage
- Optional writing of per-season and per-show MAL scores into Plex rating
  fields for Kometa overlays and native Plex sorting.
- Idempotent writes, field locking, `min_confidence` filtering, dry-run mode.

### Kometa
- Overlay file rendering MAL-blue score badges on show and season posters.

### Web UI
- FastAPI + static UI: audit view with confidence triage, MAL search and
  click-to-assign override editing, exclude management, run trigger.

### Notes
- Public Jikan API is being discontinued (Oct 1, 2026); point `jikan.base_url`
  at Tenrai or a self-hosted instance.
