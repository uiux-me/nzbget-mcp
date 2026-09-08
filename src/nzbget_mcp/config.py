"""Environment-driven configuration for the NZBGet MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

DEFAULT_USERNAME = "nzbget"
DEFAULT_PASSWORD = "tegbzn6789"
DEFAULT_PORT = 6789

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean (got {raw!r})")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number (got {raw!r})") from exc


@dataclass(frozen=True)
class IndexerSettings:
    """One configured Newznab indexer."""

    name: str
    url: str
    api_key: str
    categories: tuple[int, ...] = ()


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to reach NZBGet, search, and expose itself."""

    base_url: str
    username: str
    password: str
    timeout: float
    verify_ssl: bool
    read_only: bool
    allow_shutdown: bool
    transport: str
    host: str
    port: int
    path: str
    indexers: tuple[IndexerSettings, ...] = ()
    search_timeout: float = 20.0
    search_cache_ttl: int = 900

    @property
    def rpc_url(self) -> str:
        return f"{self.base_url}/jsonrpc"

    @property
    def search_enabled(self) -> bool:
        return bool(self.indexers)


def _resolve_base_url() -> tuple[str, str | None, str | None]:
    """Return (base_url, username, password) — credentials only if embedded in the URL."""
    raw = os.getenv("NZBGET_URL", "").strip()
    if not raw:
        scheme = os.getenv("NZBGET_SCHEME", "http").strip() or "http"
        host = os.getenv("NZBGET_HOST", "localhost").strip() or "localhost"
        port = _env_int("NZBGET_PORT", DEFAULT_PORT)
        raw = f"{scheme}://{host}:{port}"

    if "://" not in raw:
        raw = f"http://{raw}"

    parts = urlsplit(raw)
    if not parts.hostname:
        raise ValueError(f"NZBGET_URL is missing a hostname: {raw!r}")

    netloc = parts.hostname
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    # NZBGet is often mounted under a sub-path behind a reverse proxy; keep it.
    base_path = parts.path.rstrip("/")
    if base_path.endswith("/jsonrpc"):
        base_path = base_path[: -len("/jsonrpc")]

    base_url = urlunsplit((parts.scheme, netloc, base_path, "", ""))
    return base_url, parts.username, parts.password


def _load_indexers() -> tuple[IndexerSettings, ...]:
    """Read INDEXER<N>_* variables, mirroring NZBGet's own Server1.* convention.

    Scanning stops at the first gap so a commented-out block does not silently
    hide the indexers numbered after it.
    """
    if not _env_bool("SEARCH_ENABLED", True):
        return ()

    found: list[IndexerSettings] = []
    for n in range(1, 21):
        url = (os.getenv(f"INDEXER{n}_URL") or "").strip()
        if not url:
            if found or n > 1:
                break
            continue
        api_key = (os.getenv(f"INDEXER{n}_APIKEY") or "").strip()
        if not api_key:
            raise ValueError(f"INDEXER{n}_URL is set but INDEXER{n}_APIKEY is missing")
        name = (os.getenv(f"INDEXER{n}_NAME") or "").strip() or _host_of(url)

        raw_cats = (os.getenv(f"INDEXER{n}_CATEGORIES") or "").strip()
        categories: list[int] = []
        for part in raw_cats.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise ValueError(
                    f"INDEXER{n}_CATEGORIES must be numeric Newznab ids (got {part!r})"
                )
            categories.append(int(part))

        found.append(
            IndexerSettings(name=name, url=url, api_key=api_key, categories=tuple(categories))
        )

    names = [i.name for i in found]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ValueError(f"Duplicate indexer name(s): {', '.join(sorted(duplicates))}")
    return tuple(found)


def _host_of(url: str) -> str:
    """Fall back to the hostname when an indexer is given no name."""
    host = urlsplit(url if "://" in url else f"http://{url}").hostname or url
    return host.removeprefix("api.").removeprefix("www.")


def load_settings() -> Settings:
    """Build settings from the environment, raising ValueError on bad input."""
    base_url, url_user, url_password = _resolve_base_url()

    username = os.getenv("NZBGET_USERNAME") or url_user or DEFAULT_USERNAME
    password = os.getenv("NZBGET_PASSWORD") or url_password or DEFAULT_PASSWORD

    transport = (os.getenv("MCP_TRANSPORT") or "http").strip().lower()
    if transport not in {"stdio", "http", "sse", "streamable-http"}:
        raise ValueError(
            f"MCP_TRANSPORT must be one of stdio/http/sse/streamable-http (got {transport!r})"
        )

    return Settings(
        base_url=base_url,
        username=username,
        password=password,
        timeout=_env_float("NZBGET_TIMEOUT", 30.0),
        verify_ssl=_env_bool("NZBGET_VERIFY_SSL", True),
        read_only=_env_bool("NZBGET_READ_ONLY", False),
        allow_shutdown=_env_bool("NZBGET_ALLOW_SHUTDOWN", False),
        transport=transport,
        host=os.getenv("MCP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int("MCP_PORT", 8000),
        path=os.getenv("MCP_PATH", "/mcp").strip() or "/mcp",
        indexers=_load_indexers(),
        search_timeout=_env_float("SEARCH_TIMEOUT", 20.0),
        search_cache_ttl=_env_int("SEARCH_CACHE_TTL", 900),
    )
