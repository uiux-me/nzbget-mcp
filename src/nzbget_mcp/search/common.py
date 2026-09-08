"""Shared plumbing for release search.

Every indexer produces :class:`SearchResult` objects keyed by a short
``result_id``. That id is the only handle the rest of the server needs: the
NZB download URL is recovered from the cache when the caller adds it, so
search results stay small and ``add_nzb`` never has to be handed a long
signed URL full of API keys.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

USER_AGENT = "nzbget-mcp/0.1 (+https://nzbget.com)"

# Standard Newznab category ids. Indexers may add their own, but these are
# the ones every implementation agrees on.
CATEGORIES: dict[str, tuple[int, ...]] = {
    "console": (1000,),
    "movies": (2000,),
    "music": (3000,),
    "pc": (4000,),
    "tv": (5000,),
    "anime": (5070,),
    "xxx": (6000,),
    "books": (7000,),
    "other": (8000,),
}

_PUNCT = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True)
class SearchResult:
    """One release found on one indexer."""

    title: str
    indexer: str
    nzb_url: str
    guid: str = ""
    size_bytes: int | None = None
    category: str | None = None
    posted: str | None = None
    grabs: int = 0
    poster: str | None = None
    group: str | None = None
    files: int | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def result_id(self) -> str:
        """Short stable handle, unique per (indexer, release)."""
        seed = f"{self.indexer}\x00{self.guid or self.nzb_url or self.title}"
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]

    @property
    def dedupe_key(self) -> str:
        """Releases with the same normalized name are the same thing."""
        return _PUNCT.sub("", self.title.lower())

    @property
    def age_days(self) -> int | None:
        """Days since posting — the practical proxy for retention risk."""
        if not self.posted:
            return None
        try:
            posted = datetime.strptime(self.posted, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return max(0, (datetime.now(tz=timezone.utc) - posted).days)

    def to_dict(self, include_url: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "result_id": self.result_id,
            "title": self.title,
            "indexer": self.indexer,
            "size_bytes": self.size_bytes,
            "size_human": human_size(self.size_bytes),
            "posted": self.posted,
            "age_days": self.age_days,
            "grabs": self.grabs,
        }
        if self.category:
            data["category"] = self.category
        if self.files is not None:
            data["files"] = self.files
        if self.group:
            data["group"] = self.group
        if include_url:
            data["nzb_url"] = self.nzb_url
        return data


def resolve_categories(names: list[str] | None) -> list[int]:
    """Map friendly category names to Newznab ids, rejecting unknown ones."""
    if not names:
        return []
    ids: list[int] = []
    unknown: list[str] = []
    for name in names:
        key = name.strip().lower()
        if key.isdigit():
            ids.append(int(key))
        elif key in CATEGORIES:
            ids.extend(CATEGORIES[key])
        else:
            unknown.append(name)
    if unknown:
        raise ValueError(
            f"Unknown categor{'y' if len(unknown) == 1 else 'ies'}: {', '.join(unknown)}. "
            f"Known: {', '.join(sorted(CATEGORIES))} (or a numeric Newznab id)"
        )
    return ids


def parse_size(value: Any) -> int | None:
    """Coerce a byte count or a human string like '5.7 GiB' into bytes."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) or None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text) or None
    match = re.match(r"([\d.,]+)\s*([KMGTP]?)i?B", text, re.IGNORECASE)
    if not match:
        return None
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    scale = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5}[match.group(2).upper()]
    return int(number * (1024**scale)) or None


def human_size(num_bytes: Any) -> str | None:
    """Format a byte count the way a person would read it."""
    if not isinstance(num_bytes, (int, float)) or num_bytes <= 0:
        return None
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return None  # pragma: no cover - the TiB branch always returns


def parse_date(value: Any) -> str | None:
    """Normalize a unix timestamp, ISO-8601 or RFC-822 date to ``YYYY-MM-DD``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        stamp = int(value)
        if stamp <= 0:
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc).strftime("%Y-%m-%d")
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return None


def parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class Fetcher:
    """A shared httpx client handed to every indexer query."""

    def __init__(self, timeout: float = 20.0, client: httpx.AsyncClient | None = None):
        self._owned = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
        )

    async def text(self, url: str, params: dict[str, Any] | None = None) -> str:
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.text

    async def json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return json.loads(await self.text(url, params))

    async def aclose(self) -> None:
        if self._owned:
            await self._client.aclose()
