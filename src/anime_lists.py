"""Load and query the anime-lists XML mapping file.

The XML is downloaded if the local copy is missing or older than
``refresh_days``. An in-memory index keyed by tvdb_id is built on first load
for O(1) lookups.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from lxml import etree

from src.config import AnimeListsConfig

log = logging.getLogger(__name__)


@dataclass
class SeasonMapping:
    anidb_season: int
    tvdb_season: int
    start: int
    end: int
    offset: int


@dataclass
class AnimeListEntry:
    anidb_id: int
    tvdb_id: int
    tvdb_season: int  # defaulttvdbseason attribute (-1 when "a"/absolute)
    episode_offset: int  # episodeoffset attribute
    mal_id: int
    name: str
    mappings: list[SeasonMapping] = field(default_factory=list)


def _int_attr(el, name: str, default: int = 0) -> int:
    raw = el.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class AnimeLists:
    """Owns the downloaded XML and the tvdb_id index."""

    def __init__(self, cfg: AnimeListsConfig) -> None:
        self._cfg = cfg
        self._index: dict[int, list[AnimeListEntry]] = {}
        self._loaded = False

    async def ensure_loaded(self) -> None:
        """Download (if stale) and parse the XML, building the index."""
        await self._refresh_if_stale()
        self._parse()
        self._loaded = True

    def lookup(self, tvdb_id: int) -> list[AnimeListEntry]:
        """All entries matching the TVDB ID (can be multiple — one per TVDB
        season in some long-running shows)."""
        if not self._loaded:
            raise RuntimeError("AnimeLists.ensure_loaded() was never called")
        return self._index.get(tvdb_id, [])

    # ------------------------------------------------------------------ #

    def _is_stale(self) -> bool:
        path = Path(self._cfg.local_path)
        if not path.exists():
            return True
        age_days = (time.time() - path.stat().st_mtime) / 86400.0
        return age_days > self._cfg.refresh_days

    async def _refresh_if_stale(self) -> None:
        if not self._is_stale():
            log.info("anime-lists XML at %s is fresh — skipping download", self._cfg.local_path)
            return

        log.info("Downloading anime-lists XML from %s", self._cfg.url)
        async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
            resp = await client.get(self._cfg.url)
            resp.raise_for_status()

        # Write atomically so a failed download never clobbers a good copy.
        tmp_path = self._cfg.local_path + ".tmp"
        Path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "wb") as fh:
            fh.write(resp.content)
        os.replace(tmp_path, self._cfg.local_path)
        log.info("anime-lists XML saved to %s (%d bytes)", self._cfg.local_path, len(resp.content))

    def _parse(self) -> None:
        tree = etree.parse(self._cfg.local_path)
        root = tree.getroot()

        index: dict[int, list[AnimeListEntry]] = {}
        total = 0
        skipped = 0

        for anime in root.iter("anime"):
            total += 1

            tvdb_raw = anime.get("tvdbid")
            if tvdb_raw is None or not tvdb_raw.isdigit():
                # Filters tvdbid="unknown", "movie", "hentai", etc.
                skipped += 1
                continue
            tvdb_id = int(tvdb_raw)

            mal_el = anime.find("mal-id")
            if mal_el is None or mal_el.text is None:
                # Not every <anime> element has a <mal-id> child.
                skipped += 1
                continue
            try:
                # Some entries list multiple IDs; take the first.
                mal_id = int(mal_el.text.strip().split(",")[0].strip())
            except ValueError:
                skipped += 1
                continue

            anidb_raw = anime.get("anidbid") or "0"
            try:
                anidb_id = int(anidb_raw)
            except ValueError:
                anidb_id = 0

            # defaulttvdbseason can be "a" (absolute ordering). Map that to -1
            # so it never matches a real season number in step 2; date overlap
            # matching still recovers these.
            season_raw = anime.get("defaulttvdbseason")
            if season_raw is not None and season_raw.lstrip("-").isdigit():
                tvdb_season = int(season_raw)
            else:
                tvdb_season = -1

            episode_offset = _int_attr(anime, "episodeoffset", 0)

            name_el = anime.find("name")
            name = (name_el.text or "").strip() if name_el is not None else ""

            mappings: list[SeasonMapping] = []
            mapping_list = anime.find("mapping-list")
            if mapping_list is not None:
                for m in mapping_list.iter("mapping"):
                    mappings.append(
                        SeasonMapping(
                            anidb_season=_int_attr(m, "anidbseason", 0),
                            tvdb_season=_int_attr(m, "tvdbseason", 0),
                            start=_int_attr(m, "start", 0),
                            end=_int_attr(m, "end", 0),
                            offset=_int_attr(m, "offset", 0),
                        )
                    )

            entry = AnimeListEntry(
                anidb_id=anidb_id,
                tvdb_id=tvdb_id,
                tvdb_season=tvdb_season,
                episode_offset=episode_offset,
                mal_id=mal_id,
                name=name,
                mappings=mappings,
            )
            index.setdefault(tvdb_id, []).append(entry)

        self._index = index
        log.info(
            "Parsed anime-lists XML: %d entries total, %d usable, %d skipped "
            "(no MAL ID or no numeric TVDB ID), %d distinct TVDB IDs indexed",
            total,
            total - skipped,
            skipped,
            len(index),
        )
