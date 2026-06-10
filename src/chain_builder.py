"""Build the ordered main series chain from MAL relations.

Algorithm:
  1. Walk Prequel relations from the root to find the actual series root
     (the entry with no Prequel). Cap at 20 hops.
  2. From the root, walk Sequel relations recursively to collect all
     main-chain entries. Cap at 50 entries.
  3. Never follow: Side Story, Alternative Version, Summary, Spin-off,
     Adaptation, Character, Other.
  4. Fetch full metadata for each collected MAL ID.
  5. Filter out supplementary shorts/recaps (OVA/Special/Music/CM with
     ≤ 12 episodes and < 10% of the chain's max member count) — keeping
     long OVAs that are genuinely the main content (Hellsing Ultimate, FLCL).
  6. Sort by aired_from ascending; undated entries last.
"""
from __future__ import annotations

import logging
from datetime import date

from src.jikan_client import JikanClient

log = logging.getLogger(__name__)

PREQUEL_HOP_CAP = 20
SEQUEL_HOP_CAP = 50

# Relations that are never part of the main chain.
DO_NOT_FOLLOW = {
    "Side Story",
    "Alternative Version",
    "Summary",
    "Spin-off",
    "Adaptation",
    "Character",
    "Other",
}

FILTERABLE_TYPES = {"OVA", "Special", "Music", "CM"}
FILTER_MAX_EPISODES = 12
FILTER_MEMBER_RATIO = 0.10


def _related_anime_ids(relations: list[dict], relation_name: str) -> list[int]:
    """MAL IDs of anime entries under a given relation name."""
    ids: list[int] = []
    for rel in relations:
        if rel.get("relation") != relation_name:
            continue
        for entry in rel.get("entry", []):
            if entry.get("type") == "anime" and entry.get("mal_id"):
                ids.append(int(entry["mal_id"]))
    return ids


class ChainBuilder:
    def __init__(self, jikan: JikanClient) -> None:
        self._jikan = jikan

    async def build_chain(self, root_mal_id: int) -> list[dict]:
        """Return the sorted, filtered main-chain entries starting from any
        entry of the series."""
        root = await self._find_root(root_mal_id)
        chain_ids = await self._collect_sequels(root)
        entries = await self._fetch_entries(chain_ids)
        entries = self._filter_chain(entries)
        entries.sort(key=_sort_key)
        log.info(
            "Built chain from MAL %d: root %d, %d entr%s after filtering",
            root_mal_id, root, len(entries), "y" if len(entries) == 1 else "ies",
        )
        return entries

    # ------------------------------------------------------------------ #

    async def _find_root(self, mal_id: int) -> int:
        """Walk Prequel relations until an entry with no Prequel is found."""
        current = mal_id
        seen = {current}
        for _ in range(PREQUEL_HOP_CAP):
            relations = await self._jikan.get_relations(current)
            prequels = _related_anime_ids(relations, "Prequel")
            if not prequels:
                return current
            nxt = prequels[0]
            if nxt in seen:
                log.warning(
                    "Prequel loop detected at MAL %d — using %d as root", nxt, current
                )
                return current
            seen.add(nxt)
            current = nxt
        log.warning(
            "Prequel walk from MAL %d hit the %d-hop cap — using %d as root",
            mal_id, PREQUEL_HOP_CAP, current,
        )
        return current

    async def _collect_sequels(self, root: int) -> list[int]:
        """BFS over Sequel relations from the root. Caps at SEQUEL_HOP_CAP
        collected entries. Stops at any DO_NOT_FOLLOW relation."""
        collected: list[int] = [root]
        seen = {root}
        queue = [root]
        while queue and len(collected) < SEQUEL_HOP_CAP:
            current = queue.pop(0)
            relations = await self._jikan.get_relations(current)
            for seq in _related_anime_ids(relations, "Sequel"):
                if seq in seen:
                    continue
                seen.add(seq)
                collected.append(seq)
                queue.append(seq)
                if len(collected) >= SEQUEL_HOP_CAP:
                    log.warning(
                        "Sequel walk from MAL %d hit the %d-entry cap",
                        root, SEQUEL_HOP_CAP,
                    )
                    break
        return collected

    async def _fetch_entries(self, mal_ids: list[int]) -> list[dict]:
        entries: list[dict] = []
        for mal_id in mal_ids:
            entry = await self._jikan.get_series(mal_id)
            if entry is None:
                log.warning("Chain entry MAL %d returned 404 — skipping", mal_id)
                continue
            entries.append(entry)
        return entries

    @staticmethod
    def _filter_chain(entries: list[dict]) -> list[dict]:
        """Remove an entry only if ALL of: type in FILTERABLE_TYPES,
        episodes ≤ 12, members < 10% of the chain's max member count."""
        if not entries:
            return entries
        max_members = max((e.get("members") or 0) for e in entries)
        threshold = max_members * FILTER_MEMBER_RATIO

        kept: list[dict] = []
        for e in entries:
            episodes = e.get("episodes")
            members = e.get("members") or 0
            is_filterable = (
                e.get("type") in FILTERABLE_TYPES
                # An airing OVA with unknown episode count is treated as short.
                and (episodes is None or episodes <= FILTER_MAX_EPISODES)
                and members < threshold
            )
            if is_filterable:
                log.info(
                    "Filtering chain entry MAL %d '%s' (%s, %s eps, %d members)",
                    e["mal_id"], e.get("title"), e.get("type"), episodes, members,
                )
                continue
            kept.append(e)
        return kept


def _sort_key(entry: dict) -> tuple[int, date]:
    """Aired-from ascending; entries with no air date go to the end."""
    aired_from = entry.get("aired_from")
    if aired_from:
        try:
            return (0, date.fromisoformat(aired_from))
        except ValueError:
            pass
    return (1, date.max)
