"""Load and validate config.yaml and overrides.yaml.

Config is read from the path in the CONFIG_PATH environment variable
(default: /data/config.yaml). All required keys are validated up front so a
misconfigured container fails fast with a clear message instead of dying
halfway through a run.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "/data/config.yaml"

VALID_OVERRIDE_METHODS = {"direct", "weighted_avg", "shared_entry", "ova_bundle"}


class ConfigError(Exception):
    """Raised when config.yaml or overrides.yaml is missing or invalid."""


@dataclass(frozen=True)
class PlexConfig:
    url: str
    token: str
    library_name: str
    exclude: tuple = ()  # show titles (case-insensitive) or TVDB IDs to skip


@dataclass(frozen=True)
class AnimeListsConfig:
    url: str
    local_path: str
    refresh_days: int
    # The ScudLee XML has no MAL IDs; they are joined in from Fribb's
    # anime-lists JSON via the shared AniDB ID.
    mal_map_url: str = (
        "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    )
    mal_map_path: str = ""  # default: local_path with .mal-map.json suffix


@dataclass(frozen=True)
class MatchConfig:
    # Season 0 policy: "bundle" (always ova_bundle, never chain-matched),
    # "match" (legacy behavior: full matching for S0 with >6 episodes),
    # "skip" (exclude S0 entirely from output).
    season_zero: str = "bundle"
    # Movie-type MAL entries stay in the chain (they link prequel/sequel
    # hops) but are not match candidates unless this is true. Keep false
    # when movies live in a separate Plex library.
    include_movie_entries: bool = False


@dataclass(frozen=True)
class JikanConfig:
    base_url: str
    rate_limit_per_second: int
    rate_limit_per_minute: int
    retry_attempts: int
    retry_backoff_seconds: float


@dataclass(frozen=True)
class DatabaseConfig:
    path: str
    score_ttl_days: int
    mapping_ttl_days: int
    # Relation graphs (prequel/sequel topology) are essentially static on MAL,
    # unlike scores — so they get a much longer TTL than score_ttl_days.
    relations_ttl_days: int = 90


@dataclass(frozen=True)
class OutputConfig:
    path: str


@dataclass(frozen=True)
class ApplyConfig:
    """Write MAL scores into Plex rating fields for Kometa overlays."""
    enabled: bool = False
    field: str = "audience"          # audience | user — show-level rating field
    # Season-level field. Kometa can only render <<user_rating>> on season
    # posters (audience_rating is not a valid season text variable), so
    # seasons default to the user rating field.
    season_field: str = "user"       # audience | user
    show_average: str = "mean"       # mean | episode_weighted
    min_confidence: str = "low"      # low | medium | high
    lock_fields: bool = True
    dry_run: bool = False


@dataclass(frozen=True)
class Override:
    """A single manual correction from overrides.yaml.

    Always wins over any automated resolution. ``mal_ids`` is normalised to a
    list even when the YAML used the singular ``mal_id`` form.
    """

    tvdb_id: int
    season: int
    mal_ids: list[int]
    method: str  # direct | weighted_avg | ova_bundle
    note: str = ""


@dataclass(frozen=True)
class Config:
    plex: PlexConfig
    anime_lists: AnimeListsConfig
    jikan: JikanConfig
    database: DatabaseConfig
    output: OutputConfig
    match: MatchConfig
    apply: ApplyConfig
    schedule_interval_hours: int
    overrides_path: str
    overrides: list[Override] = field(default_factory=list)
    # Excludes managed via overrides.yaml (webui writes there so config.yaml
    # comments survive); merged with plex.exclude at run time.
    override_excludes: tuple = ()


def _require(section: dict[str, Any], key: str, where: str) -> Any:
    if not isinstance(section, dict) or key not in section or section[key] is None:
        raise ConfigError(f"Missing required config key '{key}' in section '{where}'")
    return section[key]


def parse_override(raw: dict[str, Any], index: int) -> Override:
    """Validate one raw override mapping. Public because the webui validates
    entries with exactly the same rules the matcher will apply."""
    if not isinstance(raw, dict):
        raise ConfigError(f"Override #{index} is not a mapping")

    tvdb_id = raw.get("tvdb_id")
    season = raw.get("season")
    if tvdb_id is None or season is None:
        raise ConfigError(f"Override #{index} must define both 'tvdb_id' and 'season'")

    # Normalise mal_id / mal_ids to a list of ints.
    mal_ids: list[int] = []
    if "mal_ids" in raw and raw["mal_ids"] is not None:
        mal_ids = [int(m) for m in raw["mal_ids"]]
    elif "mal_id" in raw and raw["mal_id"] is not None:
        mal_ids = [int(raw["mal_id"])]

    # Determine method: explicit 'method' key wins, then 'type: ova_bundle',
    # then inferred from the shape of mal_ids.
    method = raw.get("method")
    if method is None and raw.get("type") == "ova_bundle":
        method = "ova_bundle"
    if method is None:
        method = "weighted_avg" if len(mal_ids) > 1 else "direct"
    if method not in VALID_OVERRIDE_METHODS:
        raise ConfigError(f"Override #{index} has invalid method '{method}'")

    if method != "ova_bundle" and not mal_ids:
        raise ConfigError(
            f"Override #{index} with method '{method}' must define 'mal_id' or 'mal_ids'"
        )

    return Override(
        tvdb_id=int(tvdb_id),
        season=int(season),
        mal_ids=mal_ids,
        method=method,
        note=str(raw.get("note", "")),
    )


def _load_overrides(path: str) -> tuple[list[Override], tuple]:
    p = Path(path)
    if not p.exists():
        log.info("No overrides file at %s — continuing without overrides", path)
        return [], ()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse overrides file {path}: {exc}") from exc

    entries = raw.get("overrides") or []
    if not isinstance(entries, list):
        raise ConfigError(f"'overrides' in {path} must be a list")

    overrides = [parse_override(entry, i) for i, entry in enumerate(entries)]

    excludes_raw = raw.get("exclude") or []
    if not isinstance(excludes_raw, list):
        raise ConfigError(f"'exclude' in {path} must be a list")

    log.info("Loaded %d override(s), %d exclude(s) from %s",
             len(overrides), len(excludes_raw), path)
    return overrides, tuple(excludes_raw)


def load_config(path: str | None = None) -> Config:
    """Load, validate and return the full Config (including overrides)."""
    config_path = path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    p = Path(config_path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Failed to parse config file {config_path}: {exc}") from exc

    plex_raw = _require(raw, "plex", "root")
    anime_lists_raw = _require(raw, "anime_lists", "root")
    jikan_raw = _require(raw, "jikan", "root")
    database_raw = _require(raw, "database", "root")
    output_raw = _require(raw, "output", "root")
    overrides_raw = _require(raw, "overrides", "root")

    exclude_raw = plex_raw.get("exclude") or []
    if not isinstance(exclude_raw, list):
        raise ConfigError("'plex.exclude' must be a list of titles or TVDB IDs")
    plex = PlexConfig(
        url=str(_require(plex_raw, "url", "plex")),
        token=str(_require(plex_raw, "token", "plex")),
        library_name=str(_require(plex_raw, "library_name", "plex")),
        exclude=tuple(exclude_raw),
    )
    local_path = str(_require(anime_lists_raw, "local_path", "anime_lists"))
    anime_lists = AnimeListsConfig(
        url=str(_require(anime_lists_raw, "url", "anime_lists")),
        local_path=local_path,
        refresh_days=int(_require(anime_lists_raw, "refresh_days", "anime_lists")),
        mal_map_url=str(
            anime_lists_raw.get("mal_map_url")
            or AnimeListsConfig.mal_map_url
        ),
        mal_map_path=str(
            anime_lists_raw.get("mal_map_path") or local_path + ".mal-map.json"
        ),
    )
    jikan = JikanConfig(
        base_url=str(_require(jikan_raw, "base_url", "jikan")).rstrip("/"),
        rate_limit_per_second=int(_require(jikan_raw, "rate_limit_per_second", "jikan")),
        rate_limit_per_minute=int(_require(jikan_raw, "rate_limit_per_minute", "jikan")),
        retry_attempts=int(_require(jikan_raw, "retry_attempts", "jikan")),
        retry_backoff_seconds=float(_require(jikan_raw, "retry_backoff_seconds", "jikan")),
    )
    database = DatabaseConfig(
        path=str(_require(database_raw, "path", "database")),
        score_ttl_days=int(_require(database_raw, "score_ttl_days", "database")),
        mapping_ttl_days=int(_require(database_raw, "mapping_ttl_days", "database")),
        relations_ttl_days=int(database_raw.get("relations_ttl_days", 90)),
    )
    output = OutputConfig(path=str(_require(output_raw, "path", "output")))

    schedule_interval_hours = int(raw.get("schedule_interval_hours", 24))
    if schedule_interval_hours < 0:
        raise ConfigError("schedule_interval_hours must be >= 0")

    overrides_path = str(_require(overrides_raw, "path", "overrides"))
    overrides, override_excludes = _load_overrides(overrides_path)

    match_raw = raw.get("match") or {}
    season_zero = str(match_raw.get("season_zero", MatchConfig.season_zero))
    if season_zero not in ("bundle", "match", "skip"):
        raise ConfigError("'match.season_zero' must be one of: bundle, match, skip")
    match = MatchConfig(
        season_zero=season_zero,
        include_movie_entries=bool(
            match_raw.get("include_movie_entries", MatchConfig.include_movie_entries)
        ),
    )

    apply_raw = raw.get("apply") or {}
    apply_field = str(apply_raw.get("field", ApplyConfig.field))
    if apply_field not in ("audience", "user"):
        raise ConfigError("'apply.field' must be 'audience' or 'user'")
    season_field = str(apply_raw.get("season_field", ApplyConfig.season_field))
    if season_field not in ("audience", "user"):
        raise ConfigError("'apply.season_field' must be 'audience' or 'user'")
    show_average = str(apply_raw.get("show_average", ApplyConfig.show_average))
    if show_average not in ("mean", "episode_weighted"):
        raise ConfigError("'apply.show_average' must be 'mean' or 'episode_weighted'")
    min_confidence = str(apply_raw.get("min_confidence", ApplyConfig.min_confidence))
    if min_confidence not in ("low", "medium", "high"):
        raise ConfigError("'apply.min_confidence' must be low, medium or high")
    apply = ApplyConfig(
        enabled=bool(apply_raw.get("enabled", ApplyConfig.enabled)),
        field=apply_field,
        season_field=season_field,
        show_average=show_average,
        min_confidence=min_confidence,
        lock_fields=bool(apply_raw.get("lock_fields", ApplyConfig.lock_fields)),
        dry_run=bool(apply_raw.get("dry_run", ApplyConfig.dry_run)),
    )

    return Config(
        plex=plex,
        anime_lists=anime_lists,
        jikan=jikan,
        database=database,
        output=output,
        match=match,
        apply=apply,
        schedule_interval_hours=schedule_interval_hours,
        overrides_path=overrides_path,
        overrides=overrides,
        override_excludes=override_excludes,
    )
