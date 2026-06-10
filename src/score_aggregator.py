"""Compute final per-season scores.

  direct / shared_entry → the entry's own MAL score (None while airing)
  weighted_avg          → score precomputed by edge_cases (Rule A), or
                          recomputed from the entries when the match came
                          from the cache or an override (the precomputed
                          value doesn't survive a round trip through the
                          mappings table)
  ova_bundle / unresolved → None

All scores are rounded to 2 decimal places.
"""
from __future__ import annotations

import logging

from src.jikan_client import JikanClient
from src.resolver import Resolution

log = logging.getLogger(__name__)


from dataclasses import dataclass, field


@dataclass
class SeasonScore:
    season_num: int
    mal_ids: list[int]
    mal_titles: list[str]
    score: float | None
    method: str
    confidence: str
    episode_offset: int
    notes: str


class ScoreAggregator:
    def __init__(self, jikan: JikanClient) -> None:
        self._jikan = jikan

    async def aggregate(self, match: Resolution, chain: list[dict]) -> SeasonScore:
        chain_by_id = {e["mal_id"]: e for e in chain}

        async def get_entry(mal_id: int) -> dict | None:
            # Chain entries are already in memory; everything else goes
            # through the (database-cached) Jikan client.
            return chain_by_id.get(mal_id) or await self._jikan.get_series(mal_id)

        entries: list[dict | None] = []
        titles: list[str] = []
        for mal_id in match.mal_ids:
            entry = await get_entry(mal_id)
            entries.append(entry)
            if entry is None:
                log.warning(
                    "Score aggregation: MAL %d could not be fetched", mal_id
                )
                titles.append(f"MAL {mal_id}")
            else:
                titles.append(entry.get("title") or f"MAL {mal_id}")

        score: float | None = None

        if match.method in ("direct", "shared_entry") and entries and entries[0]:
            raw = entries[0].get("score")
            score = round(raw, 2) if raw is not None else None

        elif match.method == "weighted_avg":
            if match.score is not None:
                # Already computed by edge_cases.py (Rule A).
                score = round(match.score, 2)
            else:
                # Cache/override path: recompute from the entries.
                scored = [
                    e for e in entries
                    if e and e.get("score") and e.get("episodes")
                ]
                total_eps = sum(e["episodes"] for e in scored)
                if total_eps > 0:
                    score = round(
                        sum(e["score"] * e["episodes"] for e in scored) / total_eps,
                        2,
                    )

        # ova_bundle / unresolved → score stays None.

        return SeasonScore(
            season_num=match.season_num,
            mal_ids=list(match.mal_ids),
            mal_titles=titles,
            score=score,
            method=match.method,
            confidence=match.confidence,
            episode_offset=match.episode_offset,
            notes=match.notes,
        )
