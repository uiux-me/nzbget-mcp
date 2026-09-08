"""A fake NZBGet JSON-RPC endpoint the tests can drive."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from nzbget_mcp.client import NzbGetClient
from nzbget_mcp.config import IndexerSettings, Settings
from nzbget_mcp.search import Fetcher, Indexer, ReleaseSearch
from nzbget_mcp.server import build_server


def make_settings(**overrides: Any) -> Settings:
    defaults = dict(
        base_url="http://nzbget.test:6789",
        username="nzbget",
        password="tegbzn6789",
        timeout=5.0,
        verify_ssl=True,
        read_only=False,
        allow_shutdown=False,
        transport="stdio",
        host="0.0.0.0",
        port=8000,
        path="/mcp",
        indexers=(),
    )
    defaults.update(overrides)
    return Settings(**defaults)


class FakeNzbGet:
    """Records calls and replies with canned results."""

    def __init__(self, version: str = "24.3", results: dict[str, Any] | None = None):
        self.calls: list[tuple[str, list[Any]]] = []
        self.results: dict[str, Any] = {"version": version}
        self.results.update(results or {})
        self.status_code = 200
        self.error: dict[str, Any] | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        method, params = payload["method"], payload.get("params", [])
        self.calls.append((method, params))

        if self.status_code != 200:
            return httpx.Response(self.status_code, text="denied")
        if self.error is not None:
            return httpx.Response(200, json={"version": "1.1", "id": payload["id"], "error": self.error})

        result = self.results.get(method, True)
        if isinstance(result, Callable):  # type: ignore[arg-type]
            result = result(*params)
        return httpx.Response(200, json={"version": "1.1", "id": payload["id"], "result": result})

    def params_for(self, method: str) -> list[Any]:
        for name, params in self.calls:
            if name == method:
                return params
        raise AssertionError(f"{method} was never called; saw {[c[0] for c in self.calls]}")

    def client(self, settings: Settings | None = None) -> NzbGetClient:
        return NzbGetClient(settings or make_settings(), transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake() -> FakeNzbGet:
    return FakeNzbGet()


@pytest.fixture
def build(fake: FakeNzbGet, indexers: FakeIndexers):
    """Build an MCP server wired to the fakes, with optional setting overrides."""

    def _build(**overrides: Any):
        search_kwargs = overrides.pop("search_kwargs", None)
        settings = make_settings(**overrides)
        search = indexers.search(**search_kwargs) if search_kwargs is not None else None
        return build_server(settings, client=fake.client(settings), search=search)

    return _build


class FakeIndexers:
    """Serves canned Newznab responses, one body per indexer hostname."""

    @staticmethod
    def item(
        title: str,
        guid: str = "",
        size: int = 1_610_612_736,
        grabs: int = 0,
        category: str = "TV > HD",
        pub_date: str = "Wed, 19 Aug 2026 00:36:55 -0000",
        url: str | None = None,
    ) -> str:
        """One <item> as a Newznab indexer would render it."""
        guid = guid or title.lower().replace(" ", "-")
        url = url or f"https://idx.test/getnzb/{guid}.nzb"
        return f"""<item>
          <title>{title}</title>
          <guid isPermaLink="false">{guid}</guid>
          <link>https://idx.test/details/{guid}</link>
          <pubDate>{pub_date}</pubDate>
          <enclosure url="{url}" length="{size}" type="application/x-nzb"/>
          <newznab:attr name="size" value="{size}"/>
          <newznab:attr name="grabs" value="{grabs}"/>
          <newznab:attr name="category" value="{category}"/>
          <newznab:attr name="files" value="31"/>
          <newznab:attr name="group" value="alt.binaries.teevee"/>
        </item>"""


    @staticmethod
    def feed(*items: str) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<rss version="2.0" xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">'
            f"<channel>{''.join(items)}</channel></rss>"
        )

    def __init__(self) -> None:
        self.bodies: dict[str, str] = {}
        self.status: dict[str, int] = {}
        self.queries: list[tuple[str, dict[str, str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.queries.append((host, dict(request.url.params)))
        if host in self.status:
            return httpx.Response(self.status[host], text="upstream error")
        return httpx.Response(200, text=self.bodies.get(host, self.feed()))

    def params_for(self, host: str) -> dict[str, str]:
        for name, params in self.queries:
            if name == host:
                return params
        raise AssertionError(f"{host} was never queried; saw {[q[0] for q in self.queries]}")

    def search(self, indexers: list[Indexer] | None = None, **kwargs: Any) -> ReleaseSearch:
        fetcher = Fetcher(client=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))
        return ReleaseSearch(
            indexers=indexers
            or [Indexer(name="geek", url="https://geek.test", api_key="key-1")],
            fetcher=fetcher,
            **kwargs,
        )


@pytest.fixture
def indexers() -> FakeIndexers:
    return FakeIndexers()
