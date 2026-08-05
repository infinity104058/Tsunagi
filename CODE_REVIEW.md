# Tsunagi (plex-mal-matcher) — Code Review & Work Plan

**Reviewed version:** v0.3.0 (`src/__init__.py`)
**Scope:** all of `src/`, `webui/index.html`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `config.yaml.example`
**Audience:** an AI coding agent (Claude Code or similar) working in this repo, plus the maintainer.

---

> **Progress (2026-08-05):** P0-1 … P0-5, P1-3, P1-4, P1-5 and most of the P2
> list are done (headings marked ✅), each with regression tests where
> behaviour changed. Still open: P1-1 (reversible apply), P1-2 (pin deps —
> freeze from a known-good Docker build, not a dev venv), P1-6 (CI), P1-7,
> P1-8, the three P2 items that change matching behaviour (episode-offset
> inheritance, `_search_root` multi-result scan, bundle-path confidence), and
> all of Section 4.

## How to use this document

Sections 1–3 are **actionable work items**, ordered by priority. Each has a location, a
symptom, and an acceptance criterion. Work top-down; P0 items are independent of each
other and can be done in any order within their tier.

Section 4 is **architecture discussion** — proposals that change structure rather than fix
defects. Do not act on Section 4 without confirming with the maintainer first; these are
judgment calls, not bugs.

Section 5 is **context an agent needs** to avoid making things worse (invariants, gotchas,
things that look wrong but are correct).

### Ground rules for anyone (human or agent) working from this document

- **Do not change matching thresholds without tests in place first.** See P0-5. The
  constants in `season_matcher.py` and `edge_cases.py` interact; changing one without
  regression coverage is how this tool starts producing silently wrong scores.
- **Do not "clean up" the things listed in Section 5.** They look odd and are deliberate.
- Preserve existing comment quality. The comments in this codebase explain *why*, which is
  unusually good; keep that standard for new code.
- The repo has no test suite, no CI, and no linter config. Adding those is P0-5 and P1-6.

---

## 1. P0 — correctness and recoverability

These either silently produce wrong results or require manual filesystem intervention to
recover from.

### ✅ P0-1 — Web UI silently writes unusable overrides for shows with no TVDB ID

**Location:** `webui/index.html`, render loop (the `.editbtn` emitted in `rowsHtml`), and
`openModal()`.

**Symptom:** For a show whose `tvdb_id` is `null` in `output.json`, the button is rendered
as `data-tvdb="' + esc(show.tvdb_id) + '"`. `esc(null)` returns `""`, so `openModal` does
`Number("")` → `0`. The override is saved to `overrides.yaml` with `tvdb_id: 0`, passes
`_parse_override` validation, and shows a success toast — but never applies, because
`Resolver._find_override()` returns `None` whenever `show.tvdb_id is None`.

**Why it matters:** Shows without a TVDB GUID are exactly the ones that fall through to
Jikan title search and most often need manual correction. The feature fails precisely where
it is needed, with positive feedback to the user.

**Note:** the unresolved-list rendering already guards this correctly with
`u.tvdb_id != null`. The main show table does not.

**Fix — pick one:**
- *Minimal:* omit or disable the edit button when `show.tvdb_id == null`, with a tooltip
  explaining that overrides require a TVDB ID.
- *Better:* support a non-TVDB override key. The `unresolved` table already uses negated
  `plex_id` as a sentinel for this exact situation (`resolver._mark_unresolved`); reuse that
  scheme so `overrides.yaml` can address a show by `plex_id: -12345`. Requires touching
  `config.Override`, `_parse_override`, and `Resolver._find_override`.

**Acceptance:** It is impossible to save an override that cannot match. Either the UI
prevents it, or the override is keyed such that the resolver finds it.

---

### ✅ P0-2 — Stale `.matcher-running` flag wedges the UI permanently

**Location:** `src/main.py` (`run_once`, `running_flag`), `src/webui.py` (`trigger_run`).

**Symptom:** `run_once()` writes `.matcher-running` and removes it in a `finally`. That
covers exceptions but not SIGKILL, OOM-kill, or `docker compose down` mid-run. After an
ungraceful stop the flag persists, `/api/run` returns 409 forever, and the Run button stays
disabled until someone shells into the volume and deletes the file.

**Fix:** Make the flag self-expiring. Write a payload (PID + ISO timestamp) instead of an
empty string, and refresh the mtime periodically during a run (a small asyncio task, or
touch it after each show completes). Treat the flag as dead if its mtime is older than some
bound — e.g. `max(10 * 60, expected_run_time)` seconds. `webui.state()` and
`webui.trigger_run()` should both use the same staleness helper rather than bare `.exists()`.

**Acceptance:** Kill the matcher container mid-run. The UI reports "not running" within the
staleness window and the Run button works again, with no manual file deletion.

---

### ✅ P0-3 — A failed mapping download aborts the whole run even when a usable local copy exists

**Location:** `src/anime_lists.py`, `_download_if_stale()`.

**Symptom:** `resp.raise_for_status()` propagates through `ensure_loaded()` and aborts
`run_once()`. A transient GitHub failure — or the file being one day past `refresh_days` —
means no run happens at all, despite a perfectly usable XML sitting on disk. In scheduled
mode the run is skipped until the next interval; in `schedule_interval_hours: 0` mode the
container exits non-zero.

**Fix:** Wrap the download in try/except. On failure:
- if the local file exists → log a warning with the age of the local copy, and continue;
- if it does not exist → then and only then raise, since parsing is impossible.

Apply to both the ScudLee XML and the Fribb JSON (the JSON already degrades gracefully in
`_load_mal_map`, but the *download* does not).

**Acceptance:** With network egress to `raw.githubusercontent.com` blocked and a valid
stale XML on disk, a run completes normally and logs a warning.

---

### ✅ P0-4 — Schedule interval changes never take effect

**Location:** `src/main.py`, `main()`.

**Symptom:** `interval = config.schedule_interval_hours` is read once *before* the loop.
Inside the loop, `config = load_config()` is re-read each cycle with a comment stating that
edits apply without a restart — but `_sleep_until_next_run(config, interval * 3600)` uses the
stale captured value forever.

**Fix:** Read the interval from the freshly-loaded config inside the loop. Also decide what
should happen if a running container's interval is changed to `0`; currently that is
unreachable. Simplest correct behaviour: treat `0` discovered mid-loop as "run once more,
then exit", or document that `0` requires a restart.

**Acceptance:** Editing `schedule_interval_hours` in `config.yaml` changes the next sleep
duration without a container restart, and the logged interval matches.

---

### ✅ P0-5 — No test suite

**Location:** repo-wide. `.gitignore` optimistically ignores `.pytest_cache/`.

**Why this is P0:** This is a heuristic matcher with four interacting edge-case rules, three
confidence-adjustment paths, and a cascade of magic numbers (`COUR_SPLIT_EPISODE_RATIO=1.4`,
`NO_MATCH_OVERLAP_THRESHOLD=0.30`, the `0.85/0.70` episode-ratio bands,
`OVA_CLUSTER_DISTANCE_DAYS=183`, `FILTER_MEMBER_RATIO=0.10`, `FILTER_MAX_EPISODES=12`,
`OVA_*_MAX_EPISODES=6`, `SEASON_ZERO_MIN_EPISODES=3`, `SEARCH_EPISODE_TOLERANCE=0.30`).
Right now none of these can be changed with any confidence about what breaks.

**The scaffolding already exists.** `plex_applier` is explicitly split into pure
`build_plans()` and I/O `apply_to_plex()` with a docstring saying this is "so it can be
tested without a server". `edge_cases.handle()` is pure and takes plain dicts.
`season_matcher` needs only `PlexSeason` objects and chain dicts. No mocking of Plex or
Jikan is required for the valuable tests.

**Do first — highest value per line of test code:**

1. `edge_cases.handle()` — table-driven cases for each rule:
   - Rule A fires: 24-episode Plex season, 12-episode candidate, two overlapping chain
     entries → `weighted_avg`, both MAL IDs, correct episode-weighted score.
   - Rule A does *not* fire when only one chain entry overlaps (the second cour was filtered
     out of the chain) → falls through to pass-through, not a bogus split.
   - Rule A with an unscored (currently airing) overlapping entry → that entry appears in
     `mal_ids` but is excluded from the weighted average, and the note says so.
   - Rule B fires when the candidate is already in `already_assigned` → `shared_entry`.
   - Rule D marks unresolved when `candidate is None`, and when overlap is below threshold
     *and* episode counts disagree beyond the 0.5–2.0 band.
   - Rule D passes through (does not unresolve) when overlap is weak but episode counts agree.
2. `season_matcher._match_one()` / `match_seasons()`:
   - Sequential ordering: S1 resolves to entry X, S2 best-matches X → Rule B fires. This is
     the invariant the whole "never match seasons concurrently" design rests on.
   - `already_assigned` seeded from cache/override hits produces the same result as if those
     seasons had been matched in-run.
   - `season_zero` in each of `bundle` / `match` / `skip`.
   - Season with zero dated episodes → confidence reduced exactly one level, not two.
3. `plex_applier.build_plans()`:
   - `min_confidence` filtering excludes a season from *both* the season write and the show
     average.
   - `mean` vs `episode_weighted` produce the expected show rating.
   - A show whose every season is filtered out produces no plan at all (show untouched).
4. `chain_builder._filter_chain()` — the three-condition AND. A long OVA with high member
   count survives; a 2-episode recap Special with 3% of max members is dropped.
5. `config._parse_override()` — `mal_id` vs `mal_ids` normalisation, method inference,
   `type: ova_bundle`, and each `ConfigError` path.

**Fixtures:** build small hand-written chain dicts (`{"mal_id", "type", "episodes",
"score", "members", "aired_from", "aired_to"}`) rather than recording live Jikan responses.
Add one or two realistic full-franchise fixtures for the nasty cases the README calls out
(a split cour; one MAL entry spanning two Plex seasons).

**Acceptance:** `pytest` runs offline with no Plex and no network. Deliberately changing
`COUR_SPLIT_EPISODE_RATIO` or `NO_MATCH_OVERLAP_THRESHOLD` causes failures that name the
behaviour that broke.

---

## 2. P1 — robustness, reproducibility, safety

### P1-1 — `apply` is irreversible; store previous ratings

**Location:** `src/plex_applier.py` (`apply_to_plex`, `record_writes`),
`src/database.py` (`applied_ratings`, `upsert_applied_rating`).

**Symptom:** The table is documented as an "audit trail / future restore" but records only
the value written, never the value that was there before. If a bad run writes wrong scores
across the library there is nothing to roll back to — and `lock_fields: true` means Plex's
own agents will not restore them either.

**Fix:** `apply_to_plex()` already reads `current` for its idempotency check. Capture it.
Add a `previous_rating REAL` column and include it in the write tuple. Then add a small
restore path (a CLI flag such as `python -m src.restore`, or a `--revert` mode) that walks
`applied_ratings` and writes `previous_rating` back, treating `NULL` as "unset the field".

**Acceptance:** After an apply run, `applied_ratings` contains the pre-write value for every
row, and a restore command returns the library to its prior state.

---

### P1-2 — Pin dependencies

**Location:** `requirements.txt`, `Dockerfile`.

**Symptom:** Every dependency is an unbounded `>=`. The Dockerfile does a fresh
`pip install` on each `--build`. An unchanged git commit does not produce a reproducible
image, and a breaking release of plexapi, httpx, fastapi, or pydantic changes behaviour
silently — in a tool that mutates the user's Plex metadata.

**Fix:** Pin exact versions (`pip freeze` from a known-good build), or add a lockfile and a
constraints file. Keep the loose ranges in a separate `requirements.in` if you want
dependency-update tooling.

**Acceptance:** Two builds of the same commit, weeks apart, install identical versions.

---

### ✅ P1-3 — Config errors crashloop the container

**Location:** `src/main.py` (`main()` → `sys.exit(1)`), `docker-compose.yml`
(`restart: unless-stopped`).

**Symptom:** A typo in `config.yaml` produces an infinite restart loop rather than a stopped
container with one readable error.

**Fix:** Exit `0` on `ConfigError` after logging clearly, or sleep (e.g. 60s) before exiting
non-zero so the loop is slow and the log is readable. Consider `restart: on-failure:5`.

**Acceptance:** An invalid config produces a small number of clearly-logged failures, not an
endless scroll.

---

### ✅ P1-4 — Relation cache uses the wrong TTL

**Location:** `src/jikan_client.py` (`get_relations`), `src/database.py`
(`get_cached_json` uses `_score_ttl_days`).

**Symptom:** Relation graphs are cached for `score_ttl_days` (default 7). MAL
prequel/sequel topology is essentially static; scores are what change weekly. The
`chain_builder` docstring itself notes that relation walking is the most Jikan-intensive
step, so this re-does the expensive part on the cheap part's schedule.

**Fix:** Give `jikan_cache` a per-key-prefix TTL, or add `relations_ttl_days` (default 90)
to `DatabaseConfig` and pass it through `get_cached_json`. Search results (`search:*`) can
keep the shorter TTL or get their own.

**Acceptance:** A second run inside 7 days makes no `/relations` calls for shows whose
mappings expired, only `/anime/{id}` score refreshes.

---

### ✅ P1-5 — SQLite pragmas and commit granularity

**Location:** `src/database.py` (`connect()`, every `upsert_*`).

**Symptom:** No `journal_mode=WAL`, no `busy_timeout`. Every single upsert commits
individually. Five concurrent show tasks share one `aiosqlite` connection so this is *safe*
(aiosqlite serialises on one thread), but it is slower than necessary and fragile if
anything else ever opens the file.

**Fix:** In `connect()`, execute `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`.
Consider batching mapping upserts per show rather than per season.

**Acceptance:** Pragmas verified set after `connect()`; no behaviour change otherwise.

---

### P1-6 — Add a linter and CI

**Location:** repo-wide.

Current `pyflakes` output (all trivially fixable):

```
src/edge_cases.py:12       'dataclasses.field' imported but unused
src/main.py:27             'os' imported but unused
src/resolver.py:28         'src.season_matcher.SeasonMatch' imported but unused
src/resolver.py:162        local variable 'match_season' assigned but never used
src/score_aggregator.py:23 'dataclasses.field' imported but unused
src/season_matcher.py:17   'dataclasses.field' imported but unused
src/season_matcher.py:22   'src.edge_cases.parse_date' imported but unused
src/webui.py:25            'fastapi.responses.JSONResponse' imported but unused
```

`resolver.py:162` is the notable one — a walrus (`match_season := next(...)`) whose result is
discarded. The `next()` call still runs and is load-bearing (it will raise `StopIteration` if
a match's season is not in `pending`), so **delete the assignment, not the call** — or better,
replace it with an explicit lookup and a clear error.

**Fix:** Add `ruff` with a minimal config, plus a CI workflow running ruff + pytest. Forgejo
Actions is already enabled on this repo.

**Acceptance:** CI runs on push and fails on lint errors or test failures.

---

### P1-7 — Rate limiter is per-process

**Location:** `src/jikan_client.py` (`_DualWindowRateLimiter`), instantiated separately in
`main.run_once` and `webui._jikan`.

**Symptom:** Matcher and web UI each enforce the configured limits independently against the
same API. Searching in the UI during a run can roughly double the effective request rate.

**Fix:** Low-stakes against a self-hosted or Tenrai endpoint; if it matters, this is largely
solved for free by the single-process consolidation in Section 4. Otherwise, lower the
webui's configured limits, or coordinate through a shared token file.

---

### P1-8 — Plex extraction is N+1

**Location:** `src/plex_client.py`, `get_anime_shows()`.

**Symptom:** `section.all()`, then `show.seasons()` per show, then `season.episodes()` per
season — hundreds of HTTP round trips before any MAL work begins. For large libraries this
may dominate first-run time more than the Jikan rate limit does.

**Fix:** Fetch episodes in bulk (`section.search(libtype='episode')` or `show.episodes()`)
and group in memory by `parentIndex`. Verify that `originallyAvailableAt` and `index` are
populated in the bulk response before relying on it — plexapi sometimes returns partial
objects that trigger a reload per item, which would defeat the purpose.

**Acceptance:** Round trips to Plex scale with show count, not season count; measured
first-run extraction time drops.

---

## 3. P2 — small defects and tidying

> Done: season-log field, final-attempt sleep, Retry-After + jitter, the
> `_is_ova_bundle` assert, the `resolver.py:162` walrus, the `webui.py`
> imports (now `src/runstate.py` + public `parse_override`), unused imports,
> `.dockerignore`, webui healthcheck. Open: the episode-offset carry,
> `_search_root` multi-result scan, and bundle-path confidence — all three
> change matching output and deserve maintainer sign-off plus dedicated tests.

- **`plex_applier.apply_to_plex`** logs `cfg.field` in the season-level log line but writes
  using `cfg.season_field`. Log-only, but actively misleading during `dry_run`. Use
  `cfg.season_field`.
- **`jikan_client._request`** sleeps the full backoff after the *final* attempt before
  raising. Skip the sleep on the last iteration.
- **`_request` ignores `Retry-After`** on 429 and uses linear backoff with no jitter. Honour
  the header when present; add jitter.
- **`season_matcher._is_ova_bundle`** uses a bare `assert ep_date is not None` for control
  flow. Asserts are stripped under `python -O`. Replace with a filter or an explicit check.
- **`season_matcher._match_one`** carries `episode_offset` from a *discarded* anime-lists
  direct map onto a date-overlap candidate
  (`episode_offset = episode_offset if direct is not None else 0`). Rule C then attaches that
  offset to a different MAL entry than the one it was derived from. Either reset the offset
  when the candidate changes, or document why inheriting is intended.
- **`resolver._search_root`** examines only `results[0]`. If the top result fails the ±30%
  episode check but result #2 matches exactly, the show goes unresolved. Scan the first few
  results and take the first that validates.
- **`match_seasons` season-zero `bundle` path** emits `confidence="high"` for a decision that
  was never made, polluting the run summary histogram and the UI triage bar. Use a distinct
  value, or exclude `ova_bundle` from the confidence stats.
- **`webui.py` imports `_parse_override` (private) from `config` and `running_flag`/
  `wake_file` from `main`.** Importing `src.main` pulls in plexapi and the whole matcher
  graph just for two path helpers. Move `wake_file`/`running_flag` into `config.py` or a
  small `paths.py`, and promote `_parse_override` to a public name.
- **No `.dockerignore`.** Add one (`data/`, `.git/`, `__pycache__/`).
- **No healthcheck** in `docker-compose.yml` for the webui service.

---

## 4. Architecture — proposals, discuss before acting

### 4-A — Collapse the two containers into one process

Currently: two containers built from the same image, sharing `/data`, coordinating through
`.run-now` and `.matcher-running` sentinel files with a 15-second polling loop, plus a
separate `webui-cache.db` to avoid SQLite contention, plus two independent rate limiters.

That machinery buys very little. Same image, same volume, same trust domain — there is no
meaningful isolation. The stated benefit is crash isolation (matcher dies, UI survives), but
as currently written a crash *also* wedges the UI (P0-2), so the cost is being paid without
the benefit being collected.

One process — FastAPI with the scheduler as a background asyncio task — removes: both
sentinel files, the polling loop, the P0-2 failure mode entirely, the duplicate cache DB,
and the split rate limiter (P1-7). It also allows the UI to show live progress instead of a
boolean flag.

**Cost:** a matcher crash takes the UI down with it, and the run loop needs a supervising
task that restarts on exception (which `main()` already does). **Recommendation:** do it,
but only after P0-5 (tests) so the refactor is verifiable.

### 4-B — Decompose the confidence scalar

`confidence` currently conflates *corroboration strength* with *ambiguity*. The README
concedes that `low` usually means "single-season show with nothing to cross-check" rather
than "probably wrong" — but `apply.min_confidence` gates writes on that one value, so it
cannot distinguish a well-corroborated match from an unverifiable-but-probably-fine one.

Consider tracking two axes: evidence strength (how many independent signals agreed) and
ambiguity (how close the runner-up candidate was). The UI's "needs review" filter and
`min_confidence` can then key off the right one. This changes the output schema and the
`mappings` table, so it is a v0.4 change, not a patch.

### 4-C — Remove dead structures

- **`unresolved` table** — written by `_mark_unresolved`, cleaned by `delete_unresolved`,
  and `get_unresolved()` exists but is called by nothing. The UI reads the unresolved block
  from `output.json`. Either wire `get_unresolved()` into a `/api/unresolved` endpoint that
  shows history across runs (there is a real feature here — "this show has been unresolved
  for 6 weeks"), or delete the table and its three methods.
- **`applied_ratings`** — same situation. P1-1 gives it a purpose; if P1-1 is rejected,
  delete it.
- **`episode_offset`** — threaded through `anime_lists` → `season_matcher` → `edge_cases`
  (Rule C exists *solely* to pass it along) → `resolver` → `score_aggregator` → `exporter` →
  a DB column → `output.json`. Nothing consumes it. The comment says "informational for
  downstream tools (episode thumbnail matching)"; no such tool exists, and the bundled Kometa
  overlay does not read it. That is a rule, a field on five dataclasses, and a schema column
  carried for a hypothetical. Either build the consumer or delete the whole path.

### 4-D — Compute the weighted average in one place

`edge_cases.handle()` Rule A computes it, and `score_aggregator.aggregate()` recomputes it
for the cache/override path, because the precomputed value does not survive the round trip
through the `mappings` table. Two implementations of one formula that can drift. Either
persist the score alongside the mapping, or delete the Rule A computation and always compute
in the aggregator.

### 4-E — Consolidate season-zero policy

S0 handling is currently split across three files with three thresholds:
`SEASON_ZERO_MIN_EPISODES = 3` (drops S0 at extraction in `plex_client`),
`OVA_SEASON_ZERO_MAX_EPISODES = 6` (bundles it in `season_matcher`), and the
`match.season_zero` config gate — whose `match` mode is labelled "legacy" in its own
docstring. Pick one layer to own the policy, retire the legacy mode, and document the
remaining thresholds together.

### 4-F — `mapping_ttl_days: 0` as a "force re-resolve" mechanism

The README instructs users to set it to `0`, run, then set it back to `30`. That is a
command expressed as persistent configuration, and forgetting step two silently disables
mapping caching forever. Replace with a CLI flag (`--force-resolve`) and a UI button.

---

## 5. Context an agent must not break

Things that look wrong but are correct. **Do not "fix" these.**

- **Seasons within a show are matched sequentially, never concurrently.** Rule B
  (shared entry) depends on knowing what earlier seasons resolved to. `SHOW_CONCURRENCY = 5`
  parallelises across *shows* only. Do not parallelise `match_seasons`.
- **`_process_show` deliberately skips `upsert_mapping` when `resolution.from_cache` is
  true.** Rewriting would refresh `resolved_at` and make `mapping_ttl_days` meaningless.
- **`entry_window()` closes the window on Movies and single-episode entries when
  `aired_to` is null.** MAL routinely leaves `aired_to` null for films; treating that as
  open-ended makes a 2001 movie "overlap" every season since. This is load-bearing.
- **`_write_rating()` uses `item.rate()` for user ratings rather than `editUserRating`.**
  The section-edit endpoint silently ignores `userRating` on seasons. Do not "simplify".
- **`plexapi` is synchronous and is called via `asyncio.to_thread`.** Keep it off the event
  loop.
- **`output.json` and `overrides.yaml` are written tmp + `os.replace`.** Atomicity is
  required because a second process reads them. Keep it.
- **The negated `plex_id` sentinel** in the `unresolved` table exists because the PK is
  `(tvdb_id, season_num) NOT NULL` and some shows have no TVDB ID. `exporter.write()` maps
  negatives back to `null`. If you change one, change both.
- **`webui/index.html` escapes all interpolated values through `esc()`.** MAL titles and show
  titles are untrusted input. Any new rendering must do the same.
- **The chain builder never follows Side Story / Spin-off / Alternative Version.** This is
  intentional and is why franchises like *A Certain Scientific Railgun* produce sparse chains
  — which in turn is why `_is_ova_bundle` has the `OVA_BUNDLE_MAX_EPISODES` guard. Removing
  either one resurrects the bug the other was added to fix.

### Environment notes

- Runtime state lives in `/data` (config, cache DB, mappings, `output.json`). It is
  git-ignored and contains the Plex token. Never commit it, never log the token.
- Containers run as uid/gid `568:568`.
- The public Jikan API (`api.jikan.moe`) is being discontinued — brownouts from
  2026-09-01, shutdown 2026-10-01. Tests must not depend on it. `jikan.base_url` is
  configurable for exactly this reason.
- The web UI has no authentication and can write config and trigger runs. It is expected to
  sit behind a reverse proxy with auth. Do not add features that widen its blast radius
  without flagging this.

---

## Suggested order of work

1. P0-1 (silent override bug) — small, self-contained, user-visible.
2. P0-3 (stale-file fallback) — highest resilience value per line changed.
3. P0-2 (flag staleness), P0-4 (interval reload) — small.
4. P1-2 (pin deps), P1-3 (crashloop) — trivial, prevents future mystery bugs.
5. **P0-5 (tests)** — the item that determines whether this is maintainable in six months.
6. P1-1 (reversible apply), P1-4 (relation TTL), P1-5 (pragmas), P1-6 (CI).
7. P2 cleanup as a single sweep.
8. Section 4 proposals, discussed and taken one at a time, with tests already in place.
