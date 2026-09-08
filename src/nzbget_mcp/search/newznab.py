"""Newznab protocol client.

Newznab is the API nearly every usenet indexer speaks, so one client covers
them all — including the aggregators (NZBHydra2, Prowlarr), which expose a
Newznab-compatible endpoint of their own.

Responses are parsed as XML rather than JSON on purpose: XML is the format the
specification defines as default, and the JSON shapes differ between newznab,
nZEDb, NZBHydra2 and Prowlarr in ways that XML does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree

from .common import Fetcher, SearchResult, parse_date, parse_int, parse_size


class NewznabError(RuntimeError):
    """An indexer refused the request or returned something unparseable."""


@dataclass(frozen=True, slots=True)
class Indexer:
    """One configured Newznab endpoint."""

    name: str
    url: str
    api_key: str
    categories: tuple[int, ...] = ()

    @property
    def api_url(self) -> str:
        base = self.url.rstrip("/")
        return base if base.endswith("/api") else f"{base}/api"


def _local(tag: str) -> str:
    """Strip any XML namespace, leaving the bare tag name."""
    return tag.rsplit("}", 1)[-1]


def _attrs(item: ElementTree.Element) -> dict[str, str]:
    """Collect the newznab:attr name/value pairs hanging off one item."""
    found: dict[str, str] = {}
    for child in item:
        if _local(child.tag) == "attr":
            name = child.get("name")
            if name:
                found[name.lower()] = child.get("value", "")
    return found


def _child_text(item: ElementTree.Element, name: str) -> str:
    for child in item:
        if _local(child.tag) == name and child.text:
            return child.text.strip()
    return ""


#: Newznab error codes worth explaining. The distinction that matters is
#: "your key is wrong" versus "your key is fine but the account cannot do
#: this" — indexers commonly gate API access behind a paid tier, and the
#: second case is not something a config change will fix.
ERROR_HINTS = {
    "100": "the API key is wrong",
    "101": "the account is suspended or not entitled to API access — "
    "several indexers gate the API behind a paid tier",
    "102": "the account lacks API privileges",
    "200": "a required parameter was missing",
    "201": "a parameter was rejected",
    "202": "this indexer does not support that search function",
    "203": "that search function is unavailable on this indexer",
    "910": "the indexer has disabled its API",
}


def _raise_for_error(root: ElementTree.Element) -> None:
    """Newznab reports failures as HTTP 200 with an <error> body."""
    if _local(root.tag) != "error":
        return
    code = root.get("code", "?")
    description = root.get("description", "unknown error")
    hint = ERROR_HINTS.get(code)
    raise NewznabError(
        f"indexer error {code}: {description}" + (f" ({hint})" if hint else "")
    )


def parse_response(xml: str, indexer: str) -> list[SearchResult]:
    """Turn a Newznab RSS response into results, raising on an error body."""
    try:
        root = ElementTree.fromstring(xml.strip())
    except ElementTree.ParseError as exc:
        snippet = re.sub(r"\s+", " ", xml)[:160]
        raise NewznabError(f"unparseable response: {snippet}") from exc

    _raise_for_error(root)

    results: list[SearchResult] = []
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        attrs = _attrs(item)

        # The enclosure URL is the canonical NZB link; <link> is sometimes a
        # details page rather than the file itself.
        nzb_url = ""
        enclosure_length = None
        for child in item:
            if _local(child.tag) == "enclosure":
                nzb_url = child.get("url", "")
                enclosure_length = child.get("length")
                break
        nzb_url = nzb_url or _child_text(item, "link")

        title = _child_text(item, "title")
        if not title or not nzb_url:
            continue

        results.append(
            SearchResult(
                title=title,
                indexer=indexer,
                nzb_url=nzb_url,
                guid=_child_text(item, "guid"),
                size_bytes=parse_size(attrs.get("size")) or parse_size(enclosure_length),
                category=attrs.get("category") or None,
                posted=parse_date(attrs.get("usenetdate") or _child_text(item, "pubDate")),
                grabs=parse_int(attrs.get("grabs")) or 0,
                poster=attrs.get("poster") or None,
                group=attrs.get("group") or None,
                files=parse_int(attrs.get("files")),
            )
        )
    return results


async def search(
    fetcher: Fetcher,
    indexer: Indexer,
    query: str,
    categories: list[int] | None = None,
    limit: int = 100,
) -> list[SearchResult]:
    """Run one ``t=search`` query against one indexer."""
    cats = list(categories or indexer.categories)
    params: dict[str, object] = {
        "t": "search",
        "apikey": indexer.api_key,
        "extended": 1,
        "limit": limit,
    }
    if query:
        params["q"] = query
    if cats:
        params["cat"] = ",".join(str(c) for c in cats)

    body = await fetcher.text(indexer.api_url, params)
    return parse_response(body, indexer.name)
