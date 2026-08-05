# CLAUDE.md — Tsunagi (plex-mal-matcher)

Context for AI coding sessions in this repo. Read fully before editing anything.

## What this is

Resolves Plex anime seasons to MyAnimeList entries, fetches per-season MAL
scores, writes them into Plex rating fields, and renders them as Kometa poster
badges. A FastAPI web UI audits and corrects matches. Runs as two Docker
containers (matcher + webui) from one image, sharing a `/data` volume.

The core problem: Plex/TVDB and MAL disagree about what a "season" is. One Plex
season can span multiple MAL entries (cour split → `weighted_avg`); one MAL
entry can span multiple Plex seasons (`shared_entry`). The matcher reconciles
this deterministically and caches results in SQLite.

## Layout

- `src/main.py` — entrypoint, schedule loop, wake-file handling, run orchestration
- `src/config.py` — frozen-dataclass config + overrides parsing, all validation
- `src/plex_client.py` — Plex extraction → `PlexShow`/`PlexSeason`/`PlexEpisode`
- `src/anime_lists.py` — ScudLee XML + Fribb JSON download/parse (TVDB↔MAL join)
- `src/jikan_client.py` — Jikan-v4-compatible API client, dual-window rate limiter
- `src/chain_builder.py` — MAL relation chain (prequel/sequel walk)
- `src/season_matcher.py` — season→entry matching, OVA detection, S0 policy
- `src/edge_cases.py` — rules A–D (cour split, shared entry, offset, unresolved) + date helpers
- `src/resolver.py` — per-show orchestration: override → cache → anime-lists → search
- `src/score_aggregator.py` / `src/exporter.py` — scores → `output.json`
- `src/plex_applier.py` — writes ratings into Plex (pure `build_plans` + I/O `apply_to_plex`)
- `src/webui.py` + `webui/index.html` — FastAPI API + single-file frontend
- `tests/` — offline pytest suite; `kometa/` — overlay YAML shipped to Kometa

## Workflow rules

- Work on `dev`. `main` only moves via release merges. Version lives in
  `src/__init__.py` (`__version__`) and must match the git tag at release.
- **Run `pytest` after every change.** The suite is offline — no Plex, no
  network, no live API. Keep it that way; never add a test that talks to a real
  service. Fixtures are hand-written chain dicts (see `tests/conftest.py`).
- Do not change matching thresholds (`COUR_SPLIT_EPISODE_RATIO`,
  `NO_MATCH_OVERLAP_THRESHOLD`, the 0.85/0.70 episode-ratio bands,
  `OVA_*` constants, `SEARCH_EPISODE_TOLERANCE`) without a regression test that
  names the behavior being protected. These constants interact.
- Preserve comment quality: comments here explain *why*, not what. New code
  meets that bar.
- Runtime state lives in `/data` (git-ignored): user config with the Plex
  token, SQLite cache, mappings, `output.json`. Never commit it, never log the
  token, never add an API response that serializes config
  (`tests/test_webui.py::test_state_never_leaks_token` enforces this).
- The web UI has **no authentication** and can write config and trigger runs.
  It is deployed behind a reverse proxy with auth. Do not add features that
  widen its blast radius (container control, arbitrary file access, shell
  execution) — flag any such request to the maintainer instead.
- `CODE_REVIEW.md` (if present) is the active work plan. Its Section 4 items
  are proposals: **discuss with the maintainer before acting on them.**

## Invariants — things that look wrong but are correct. Do NOT "fix" these.

- **Seasons within a show are matched sequentially, never concurrently.**
  Rule B (shared entry) depends on knowing what earlier seasons resolved to.
  `SHOW_CONCURRENCY` parallelises across shows only. Never parallelise
  `match_seasons`.
- **`_process_show` skips `upsert_mapping` when `resolution.from_cache` is
  true.** Rewriting would refresh `resolved_at` and make `mapping_ttl_days`
  meaningless.
- **`entry_window()` closes the air window for Movies / single-episode entries
  with null `aired_to`.** MAL routinely leaves `aired_to` null for films;
  treating that as open-ended made a 2001 movie "overlap" every season since
  (a real production bug). Load-bearing.
- **`_write_rating()` uses `item.rate()` for user ratings, not
  `editUserRating`.** Plex's section-edit endpoint silently ignores
  `userRating` on seasons; only the `/:/rate` endpoint works there. Do not
  "simplify" to the mixin method.
- **Show ratings go to `audienceRating`; season ratings go to `userRating`.**
  Not stylistic: Kometa can only render `<<user_rating>>` as a season-level
  text variable (`audience_rating` is movie/show/episode only). Single-season
  shows get both writes with the same value.
- **The chain builder never follows Side Story / Spin-off / Alternative
  Version relations.** Intentional — it prevents wandering into unrelated
  franchises. It also means franchises like *A Certain Scientific Railgun*
  produce sparse/broken chains, which is why `_is_ova_bundle` has the
  `OVA_BUNDLE_MAX_EPISODES` guard (a full-length season is never an OVA bundle
  regardless of chain distance). Removing either side resurrects the bug the
  other was added to fix.
- **`plexapi` is synchronous and is called via `asyncio.to_thread`.** Keep it
  off the event loop.
- **`output.json` and `overrides.yaml` are written tmp-file + `os.replace`.**
  A second process reads them; atomicity is required.
- **Negated `plex_id` sentinel** in the `unresolved` table exists because the
  PK requires a non-null TVDB ID and some shows have none. `exporter.write()`
  maps negatives back to `null`. Change both or neither.
- **`webui/index.html` escapes every interpolated value through `esc()`.**
  Show titles and MAL titles are untrusted. Any new rendering must do the same.
- **Overrides always win over cache**, and config is reloaded every schedule
  cycle so override edits apply without restart. Preserve both properties.

## External-world facts that shape the code

- The public Jikan API (`api.jikan.moe`) shuts down 2026-10-01 (brownouts from
  09-01). `jikan.base_url` points at Tenrai (`https://api.tenrai.org/v1`), a
  drop-in Jikan-v4 mirror. Never hardcode an API host; never make tests depend
  on any live endpoint.
- The ScudLee anime-lists XML contains **no MAL IDs**. They are joined from
  Fribb's JSON via AniDB ID (`_load_mal_map`). This join is what makes the
  anime-lists resolution step work at all.
- Jikan-style 5xx responses (500/502/504) are routine upstream failures, not
  client errors — they are in `RETRYABLE_STATUS` deliberately.
- Kometa tracks overlay state via an `Overlay` label on Plex items plus a
  marker in the poster file. The matcher does not interact with these, and
  must not try to.
- Containers run as uid/gid `568:568`; appdata paths are owned accordingly.

## Deploy reality (for context; sessions don't deploy)

Maintainer flow: edit locally → pytest → commit to `dev` → push to Forgejo →
on the NAS: `git pull && docker compose up -d --build` (source is baked into
the image; a restart without `--build` does not pick up code changes). Config
changes in `/data/config.yaml` need no rebuild. Forced full re-resolution:
`mapping_ttl_days: 0` for one run (a `--force-resolve` flag is planned to
replace this).
