"""Fan search out across the configured indexers and merge what comes back."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .common import Fetcher, SearchResult
from .newznab import Indexer, search as newznab_search

logger = logging.getLogger("nzbget_mcp.search")


class UnknownIndexerError(ValueError):
    """Raised when a caller names an indexer that is not configured."""


class ResultCache:
    """TTL cache of results by id, so add_nzb can recover the NZB URL."""

    def __init__(self, ttl: int = 900):
        self._ttl = ttl
        self._entries: dict[str, tuple[float, SearchResult]] = {}

    def put(self, results: list[SearchResult]) -> None:
        now = time.monotonic()
        for result in results:
            self._entries[result.result_id] = (now, result)
        self._evict(now)

    def get(self, result_id: str) -> SearchResult | None:
        entry = self._entries.get(result_id.strip().lower())
        if not entry:
            return None
        stored_at, result = entry
        if time.monotonic() - stored_at > self._ttl:
            self._entries.pop(result_id.strip().lower(), None)
            return None
        return result

    def _evict(self, now: float) -> None:
        deadline = now - self._ttl
        self._entries = {
            key: entry for key, entry in self._entries.items() if entry[0] > deadline
        }

    def __len__(self) -> int:
        return len(self._entries)


class ReleaseSearch:
    """Searches every configured indexer in parallel and merges the results."""

    def __init__(
        self,
        indexers: list[Indexer],
        timeout: float = 20.0,
        cache_ttl: int = 900,
        fetcher: Fetcher | None = None,
    ) -> None:
        if not indexers:
            raise ValueError("ReleaseSearch needs at least one indexer")
        seen: set[str] = set()
        for indexer in indexers:
            if indexer.name in seen:
                raise ValueError(f"Duplicate indexer name: {indexer.name}")
            seen.add(indexer.name)
        self.indexers = list(indexers)
        self._by_name = {i.name: i for i in self.indexers}
        self._fetcher = fetcher or Fetcher(timeout=timeout)
        self.cache = ResultCache(cache_ttl)

    def available_indexers(self) -> list[dict[str, Any]]:
        return [
            {
                "name": i.name,
                "url": i.url,
                "categories": list(i.categories) or "all",
            }
            for i in self.indexers
        ]

    def resolve(self, requested: list[str] | None) -> list[Indexer]:
        """Validate an explicit indexer list against what is configured."""
        if not requested:
            return self.indexers
        unknown = [name for name in requested if name not in self._by_name]
        if unknown:
            raise UnknownIndexerError(
                f"Unknown indexer(s): {', '.join(sorted(unknown))}. "
                f"Configured: {', '.join(self._by_name)}"
            )
        return [self._by_name[name] for name in requested]

    async def search(
        self,
        query: str,
        indexers: list[str] | None = None,
        categories: list[int] | None = None,
        limit: int = 100,
    ) -> tuple[list[SearchResult], dict[str, Any]]:
        """Search the given indexers, returning (results, per-indexer report)."""
        targets = self.resolve(indexers)
        query = query.strip()

        gathered = await asyncio.gather(
            *(
                newznab_search(self._fetcher, indexer, query, categories, limit)
                for indexer in targets
            ),
            return_exceptions=True,
        )

        report: dict[str, Any] = {}
        merged: dict[str, SearchResult] = {}
        for indexer, outcome in zip(targets, gathered):
            if isinstance(outcome, BaseException):
                logger.warning("indexer %s failed for %r: %s", indexer.name, query, outcome)
                report[indexer.name] = {"ok": False, "error": str(outcome)[:200]}
                continue
            report[indexer.name] = {"ok": True, "results": len(outcome)}
            for result in outcome:
                # The same release is often carried by several indexers; keep
                # whichever copy has been grabbed most, as the better bet.
                existing = merged.get(result.dedupe_key)
                if existing is None or result.grabs > existing.grabs:
                    merged[result.dedupe_key] = result

        # Usenet has no seeder count. Grabs are the closest thing to proof a
        # release is complete and downloadable; recency breaks ties because
        # retention makes older posts likelier to be incomplete.
        results = sorted(
            merged.values(),
            key=lambda r: (r.grabs, r.posted or "", r.size_bytes or 0),
            reverse=True,
        )
        self.cache.put(results)
        return results, report

    async def aclose(self) -> None:
        await self._fetcher.aclose()
