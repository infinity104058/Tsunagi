# plex-mal-matcher

Resolve the anime seasons in a Plex library to their [MyAnimeList](https://myanimelist.net)
entries, fetch per-season MAL scores, and surface those scores as poster
badges via [Kometa](https://kometa.wiki) — with a web UI for auditing and
correcting matches.

The hard part of "put the MAL score on the poster" is that Plex, TheTVDB, and
MAL disagree about what a *season* is. A single Plex season can be a cour split
across two MAL entries; one MAL entry can span two Plex seasons; movies and OVAs
muddy the mapping further. This tool does the reconciliation deterministically,
caches the result, and lets you override anything it gets wrong.

---

## What it does

1. Reads every anime show and season from a Plex library.
2. Resolves each **Plex season** to one or more **MAL entries**, in priority
   order: manual overrides → cache → the [anime-lists](https://github.com/Anime-Lists/anime-lists)
   TVDB↔MAL mapping → Jikan title search → unresolved.
3. Fetches each entry's MAL score and computes a per-season score (a weighted
   average when a season spans multiple entries).
4. Writes the result to `output.json`.
5. *(Optional)* Writes those scores into Plex's rating fields so Kometa can
   render them as poster badges.

A companion **web UI** shows every match with a confidence rating, lets you
search MAL and reassign any season by hand, manage an exclude list, and trigger
a re-run.

---

## Architecture

Two containers built from one image, sharing a `/data` volume:

| Container | Role |
|-----------|------|
| `plex-mal-matcher` | The matcher. Runs on a schedule (or once), writes `output.json`, optionally applies ratings to Plex. |
| `plex-mal-matcher-webui` | FastAPI + static UI on port `8484`. Reads `output.json`, writes `overrides.yaml`, triggers runs. |

Everything the matcher needs — cache, config, mappings, output — lives in the
`/data` volume as plain files and a SQLite database. Nothing is stored inside
the image.

### Data sources

- **Plex** — the library structure (shows, seasons, episode air dates).
- **anime-lists** (ScudLee XML) — TVDB↔AniDB season mapping. Downloaded, cached.
- **Fribb's anime-lists** (JSON) — AniDB→MAL ID join. The XML has no MAL IDs of
  its own; this supplies them. Downloaded, cached.
- **A Jikan-compatible API** — live MAL scores, episode counts, and relation
  chains. See the note on Jikan below.

---

## Requirements

- Docker + Docker Compose (this README assumes [Dockge](https://dockge.kuma.pet)
  on TrueNAS, but any compose host works).
- A Plex server with an anime library and a
  [Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).
- Network access to GitHub (for the mapping files) and a Jikan-compatible API.
- *(For poster badges)* Kometa, plus a free [TMDb](https://www.themoviedb.org)
  API key (Kometa requires one even though this tool doesn't use TMDb data).

---

## A note on the Jikan API

This tool talks to a [Jikan v4](https://docs.api.jikan.moe/)-compatible API for
live MAL data. **The public Jikan API (`api.jikan.moe`) is being discontinued —
maintenance-only since June 2026, brownouts from September 1, 2026, and fully
shut down on October 1, 2026.** Its search endpoint is already unreliable.

Point `jikan.base_url` at a working instance instead. Options:

- **[Tenrai](https://tenrai.org)** — a drop-in Jikan v4-compatible mirror
  (`https://api.tenrai.org/v1`). No code changes; just the base URL.
- **Self-hosted Jikan REST** — run your own instance and point at it. Heavier,
  but fully under your control.

The response schema is identical across these; only the base URL changes.

---

## Quick start

### 1. Get the source into your stack directory

```bash
git clone https://your.gitea/you/plex-mal-matcher.git plex-mal-matcher
cd plex-mal-matcher
```

The `docker-compose.yml` uses `build: .`, so the source lives in the stack
directory alongside the compose file.

### 2. Create the data directory

```bash
mkdir -p data
sudo chown -R 568:568 data   # container runs as uid/gid 568:568
```

`data/` is git-ignored — it holds runtime state (config with your token, the
cache DB, downloaded mappings, output). It is never committed.

### 3. Create your config

```bash
cp config.yaml.example data/config.yaml
cp overrides.yaml.example data/overrides.yaml
sudo chown 568:568 data/*.yaml
```

Edit `data/config.yaml`:

- `plex.url` — your Plex server (container name or LAN IP, **never** localhost).
- `plex.token` — your Plex token.
- `plex.library_name` — the exact library section name (e.g. `Anime Shows`).
- `jikan.base_url` — a working Jikan-compatible endpoint (see the note above).
- `schedule_interval_hours` — `0` to run once and exit; `24` for a daily loop.

Every other key has a sensible default; see
[Configuration](#configuration) for the full reference.

### 4. Deploy

```bash
docker compose up -d --build
```

This builds the image once and starts both containers. Watch the matcher's
logs — the first run downloads the mapping files (~30 MB) and resolves your
library at the API's rate limit, so it takes a while. Subsequent runs are fast
(everything is cached).

### 5. Open the web UI

Visit `http://<host>:8484`. **The UI has no authentication of its own and can
write config and trigger runs — put it behind a reverse proxy with auth (e.g.
Authentik) before exposing it beyond your LAN.**

---

## Configuration

Full reference — see `config.yaml.example` for the annotated version.

### `plex`
| Key | Description |
|-----|-------------|
| `url` | Plex base URL. Container name or LAN IP, never localhost. |
| `token` | Plex auth token. |
| `library_name` | Exact Plex library section name. |
| `exclude` | List of show titles (case-insensitive) or TVDB IDs to skip entirely. |

### `anime_lists`
| Key | Description |
|-----|-------------|
| `url` | anime-lists XML URL. |
| `local_path` | Where to cache the XML. |
| `refresh_days` | Re-download when older than this. |
| `mal_map_url` | Fribb JSON URL (AniDB→MAL join). |
| `mal_map_path` | Where to cache it (default: alongside the XML). |

### `match`
| Key | Description |
|-----|-------------|
| `season_zero` | `bundle` (default — S0 is always a specials bundle, unscored), `match` (attempt to resolve S0 with 7+ episodes), or `skip` (omit S0 entirely). |
| `include_movie_entries` | `false` (default) keeps Movie-type MAL entries out of season matching — correct when movies live in a separate Plex library. `true` allows them as candidates. |

### `jikan`
| Key | Description |
|-----|-------------|
| `base_url` | Jikan v4-compatible API base URL. |
| `rate_limit_per_second` / `rate_limit_per_minute` | Client-side pacing. Applies to every request including retries. |
| `retry_attempts` / `retry_backoff_seconds` | Retry policy for transient (429/5xx) responses. |

### `schedule_interval_hours`
`0` runs once and exits. Any positive number loops with that interval; the web
UI's "Run now" button wakes the loop early.

### `database`
| Key | Description |
|-----|-------------|
| `path` | SQLite DB path. |
| `score_ttl_days` | Re-fetch MAL scores older than this (default 7). |
| `mapping_ttl_days` | Re-resolve TVDB→MAL mappings older than this (default 30). Set to `0` for one run to force a full re-resolution. |

### `output`
| Key | Description |
|-----|-------------|
| `path` | Where `output.json` is written. |

### `apply`
Writes MAL scores into Plex rating fields for Kometa overlays. See
[Kometa integration](#kometa-integration).

| Key | Description |
|-----|-------------|
| `enabled` | `false` by default. `true` writes ratings after each run. |
| `field` | Show-level rating field: `audience` or `user`. |
| `season_field` | Season-level field. Must be `user` — Kometa can only render `<<user_rating>>` on season posters. |
| `show_average` | `mean` or `episode_weighted`. A single-season show just gets that season's score. |
| `min_confidence` | `low` / `medium` / `high`. Seasons below this are not rated. |
| `lock_fields` | Lock the field so Plex refreshes don't revert it. |
| `dry_run` | Log what would change, write nothing. |

### `overrides`
| Key | Description |
|-----|-------------|
| `path` | Path to `overrides.yaml`. |

---

## Overrides

`overrides.yaml` always wins over automatic resolution. You can edit it by hand
or manage it entirely from the web UI. Each entry maps one TVDB season:

```yaml
overrides:
  # Direct match: one Plex season → one MAL entry
  - tvdb_id: 73255
    season: 1
    mal_id: 5114
    note: "FMA Brotherhood"

  # Cour split: one Plex season → several MAL entries (weighted by episode count)
  - tvdb_id: 400455
    season: 4
    mal_ids: [40028, 51535]
    method: weighted_avg

  # OVA bundle: season listed but not scored
  - tvdb_id: 83462
    season: 0
    type: ova_bundle

# Shows skipped entirely (same effect as plex.exclude)
exclude: []
```

---

## How matching works

Per Plex season, in order:

1. **Override** — if `overrides.yaml` has an entry, use it.
2. **Cache** — if a fresh mapping exists in the DB, use it.
3. **anime-lists direct map** — map the TVDB season to a MAL entry via the
   XML + Fribb join.
4. **Air-date / episode-count matching** — build the show's relation chain
   (walking prequel/sequel links, never side stories) and match seasons to
   entries by air-date overlap and episode count. Detects cour splits
   (one season → many entries) and shared entries (one entry → many seasons).
5. **Jikan title search** — last-resort fuzzy match, validated against episode
   count.
6. **Unresolved** — recorded for you to fix manually.

### Confidence

Each match gets `high` / `medium` / `low`. **Confidence measures corroboration,
not correctness.** A single-season show with one MAL entry and no relation chain
to cross-check often lands at `low` even when the match is obviously right —
there simply wasn't extra evidence to promote it. Treat `low` as "worth a
glance," not "wrong." The web UI's *needs review* filter surfaces exactly the
matches worth confirming (low confidence, cour splits, shared entries,
unresolved).

---

## Kometa integration

When `apply.enabled: true`, the matcher writes each season's score to Plex's
user-rating field and each show's score (the average of its seasons) to the
audience-rating field. The included overlay file renders these as MAL-blue
badges.

1. Enable `apply` in config. Do a `dry_run: true` pass first and check the logs.
2. Copy `kometa/overlays-anime-ratings.yml` into your Kometa config directory.
3. Reference it in Kometa's `config.yml`:
   ```yaml
   libraries:
     Anime Shows:
       overlay_files:
         - file: config/overlays-anime-ratings.yml
   ```
4. Run Kometa.

> **Important:** Do **not** enable Kometa's `mass_audience_rating_update`,
> `mass_user_rating_update`, or related operations on this library. They will
> overwrite the per-season scores this tool writes, on every Kometa run.

Because scores are written into Plex, they also show up natively in the Plex UI
and become sortable — you can sort the library by MAL score without Kometa at
all.

---

## Deploying updates

Config lives in `data/` and is never touched by a pull, so updating is:

```bash
git pull
docker compose up -d --build   # --build because source changed
```

When a release adds new config keys, they ship with defaults — diff your
`data/config.yaml` against `config.yaml.example` to see what's newly available;
you never need to overwrite your working config.

To force re-resolution of the whole library after a matching change, set
`mapping_ttl_days: 0`, run once, then set it back to `30`.

---

## License

MIT — see [LICENSE](LICENSE).
