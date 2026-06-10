"""Write the structured output JSON that Kometa consumes.

The file is written atomically (tmp + rename) so a crash mid-write never
leaves Kometa reading a truncated file.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from src.database import utc_now_iso
from src.score_aggregator import SeasonScore

log = logging.getLogger(__name__)


@dataclass
class ShowResult:
    plex_id: int
    title: str
    tvdb_id: int | None
    resolution_source: str
    seasons: dict[int, SeasonScore] = field(default_factory=dict)
    status: str = "complete"  # complete | partial | unresolved


def write(
    results: list[ShowResult],
    unresolved: list[dict],
    path: str,
) -> None:
    """Serialise results + unresolved entries to the output path."""
    shows: dict[str, dict] = {}
    for result in results:
        shows[result.title] = {
            "plex_id": result.plex_id,
            "tvdb_id": result.tvdb_id,
            "resolution_source": result.resolution_source,
            "status": result.status,
            "seasons": {
                str(season_num): {
                    "mal_ids": score.mal_ids,
                    "mal_titles": score.mal_titles,
                    "score": score.score,
                    "method": score.method,
                    "confidence": score.confidence,
                    "episode_offset": score.episode_offset,
                    "notes": score.notes,
                }
                for season_num, score in sorted(result.seasons.items())
            },
        }

    unresolved_block = [
        {
            "title": u.get("title"),
            # Negative IDs are the no-TVDB sentinel — map back to null.
            "tvdb_id": u["tvdb_id"] if (u.get("tvdb_id") or 0) > 0 else None,
            "reason": u.get("reason"),
        }
        for u in unresolved
    ]

    payload = {
        "generated_at": utc_now_iso(),
        "show_count": len(shows),
        "unresolved_count": len(unresolved_block),
        "shows": shows,
        "unresolved": unresolved_block,
    }

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(out) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, out)
    log.info(
        "Wrote %s: %d show(s), %d unresolved", path, len(shows), len(unresolved_block)
    )
