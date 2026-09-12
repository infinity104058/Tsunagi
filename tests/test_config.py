"""Config loading, validation, and override parsing."""
from __future__ import annotations

import pytest

from src.config import ConfigError, _load_overrides, load_config, parse_override

BASE_CONFIG = """
plex: {{url: "http://x:32400", token: "t", library_name: "Anime Shows"{exclude}}}
anime_lists: {{url: "http://x", local_path: "{tmp}/al.xml", refresh_days: 7}}
jikan: {{base_url: "http://j", rate_limit_per_second: 1, rate_limit_per_minute: 45, retry_attempts: 5, retry_backoff_seconds: 3.0}}
database: {{path: "{tmp}/m.db", score_ttl_days: 7, mapping_ttl_days: 30}}
output: {{path: "{tmp}/o.json"}}
overrides: {{path: "{tmp}/ov.yaml"}}
{match}
"""


def write_config(tmp_path, match="", exclude=""):
    p = tmp_path / "config.yaml"
    # as_posix(): Windows backslashes are escape sequences inside the
    # double-quoted YAML scalars in BASE_CONFIG.
    p.write_text(BASE_CONFIG.format(tmp=tmp_path.as_posix(), match=match, exclude=exclude))
    return str(p)


def test_minimal_config_defaults(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.match.season_zero == "bundle"
    assert cfg.match.include_movie_entries is False
    assert cfg.apply.enabled is False
    assert cfg.apply.season_field == "user"
    assert cfg.plex.exclude == ()
    assert "tenrai" in cfg.anime_lists.mal_map_url.lower() or "fribb" in cfg.anime_lists.mal_map_url.lower()


def test_exclude_list_parsed(tmp_path):
    cfg = load_config(write_config(tmp_path, exclude=', exclude: ["One Piece", 81797]'))
    assert cfg.plex.exclude == ("One Piece", 81797)


def test_match_section(tmp_path):
    cfg = load_config(write_config(
        tmp_path, match='match: {season_zero: "skip", include_movie_entries: true}'))
    assert cfg.match.season_zero == "skip"
    assert cfg.match.include_movie_entries is True


def test_invalid_season_zero_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, match='match: {season_zero: "bogus"}'))


def test_apply_section(tmp_path):
    cfg = load_config(write_config(
        tmp_path,
        match='apply: {enabled: true, field: audience, show_average: episode_weighted, min_confidence: medium, dry_run: true}'))
    assert cfg.apply.enabled is True
    assert cfg.apply.show_average == "episode_weighted"
    assert cfg.apply.min_confidence == "medium"
    assert cfg.apply.dry_run is True


def test_invalid_apply_field_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, match='apply: {field: bogus}'))


def test_rating_image_default_and_custom(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.apply.rating_image == "imdb://image.rating"
    cfg = load_config(write_config(
        tmp_path, match='apply: {rating_image: "themoviedb://image.rating"}'))
    assert cfg.apply.rating_image == "themoviedb://image.rating"


def test_rating_image_empty_disables(tmp_path):
    cfg = load_config(write_config(tmp_path, match='apply: {rating_image: ""}'))
    assert cfg.apply.rating_image == ""


def test_rating_image_bare_word_rejected(tmp_path):
    """A non-URI value would be silently ignored by Plex — fail fast instead."""
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, match='apply: {rating_image: "imdb"}'))


# ------------------------------------------------------------ override parse

def test_parse_override_direct():
    ov = parse_override({"tvdb_id": 100, "season": 1, "mal_id": 5114}, 0)
    assert ov.method == "direct"
    assert ov.mal_ids == [5114]


def test_parse_override_cour_split():
    ov = parse_override({"tvdb_id": 100, "season": 4, "mal_ids": [40028, 51535],
                          "method": "weighted_avg"}, 0)
    assert ov.method == "weighted_avg"
    assert ov.mal_ids == [40028, 51535]


def test_parse_override_ova_bundle_via_type():
    ov = parse_override({"tvdb_id": 100, "season": 0, "type": "ova_bundle"}, 0)
    assert ov.method == "ova_bundle"


def test_parse_override_direct_without_ids_rejected():
    with pytest.raises(ConfigError):
        parse_override({"tvdb_id": 100, "season": 2, "mal_ids": []}, 0)


def test_load_overrides_with_excludes(tmp_path):
    ov = tmp_path / "ov.yaml"
    ov.write_text(
        "overrides:\n"
        "  - {tvdb_id: 100, season: 1, mal_id: 5114}\n"
        "exclude: [\"One Piece\", 81797]\n"
    )
    overrides, excludes = _load_overrides(str(ov))
    assert len(overrides) == 1
    assert excludes == ("One Piece", 81797)


def test_load_overrides_missing_file(tmp_path):
    overrides, excludes = _load_overrides(str(tmp_path / "nope.yaml"))
    assert overrides == [] and excludes == ()


