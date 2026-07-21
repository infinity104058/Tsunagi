# Changelog

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
