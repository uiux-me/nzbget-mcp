"""FastMCP server exposing NZBGet's JSON-RPC API as MCP tools."""

from __future__ import annotations

import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .client import NzbGetClient, NzbGetError
from .config import Settings, load_settings
from .search import (
    Indexer,
    ReleaseSearch,
    UnknownIndexerError,
    resolve_categories,
)
from .normalize import (
    merge_hi_lo,
    strip_deprecated,
    summarize_file,
    summarize_group,
    summarize_history,
    summarize_log,
    summarize_status,
)

INSTRUCTIONS = """\
Control a NZBGet usenet downloader through its JSON-RPC API.

Use `get_status` for an at-a-glance summary (speed, pause state, disk space),
`list_queue` for active downloads and `get_history` for finished ones. Downloads
are identified by `nzb_id`, which every listing returns. Act on them with
`manage_downloads` (queue) and `manage_history` (history). Sizes are bytes and
timestamps are ISO-8601 UTC unless stated otherwise.

When search is configured, the usual flow is two steps: `search_releases` to
find candidates, then `add_nzb` with the winning `result_id`. Usenet has no
seeder count, so judge candidates by `grabs` (how many people have taken it,
the best proxy for a complete post) and `age_days` (older posts are likelier
to have decayed past retention).
"""

#: Connection defaults for providers people commonly subscribe to. Only the
#: transport settings — credentials always come from the caller.
PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "newshosting": {
        "host": "news.newshosting.com",
        "port": 563,
        "encryption": True,
        "connections": 30,
    },
    "usenetserver": {
        "host": "news.usenetserver.com",
        "port": 563,
        "encryption": True,
        "connections": 20,
    },
    "eweka": {
        "host": "news.eweka.nl",
        "port": 563,
        "encryption": True,
        "connections": 20,
    },
}

CERT_LEVELS = {"none": 0, "minimal": 1, "strict": 2}

# Friendly action name -> (editqueue command, requires a `param` value)
DOWNLOAD_ACTIONS: dict[str, tuple[str, bool]] = {
    "pause": ("GroupPause", False),
    "resume": ("GroupResume", False),
    "delete": ("GroupDelete", False),
    "delete_permanently": ("GroupFinalDelete", False),
    "delete_as_duplicate": ("GroupDupeDelete", False),
    "move_top": ("GroupMoveTop", False),
    "move_bottom": ("GroupMoveBottom", False),
    "move_offset": ("GroupMoveOffset", True),
    "merge": ("GroupMerge", False),
    "set_priority": ("GroupSetPriority", True),
    "set_category": ("GroupSetCategory", True),
    "apply_category": ("GroupApplyCategory", True),
    "set_name": ("GroupSetName", True),
    "set_parameter": ("GroupSetParameter", True),
    "set_dupe_key": ("GroupSetDupeKey", True),
    "set_dupe_score": ("GroupSetDupeScore", True),
    "set_dupe_mode": ("GroupSetDupeMode", True),
    "sort": ("GroupSort", True),
}

HISTORY_ACTIONS: dict[str, tuple[str, bool]] = {
    "hide": ("HistoryDelete", False),
    "delete_permanently": ("HistoryFinalDelete", False),
    "return_to_queue": ("HistoryReturn", False),
    "redownload": ("HistoryRedownload", False),
    "post_process_again": ("HistoryProcess", False),
    "mark_good": ("HistoryMarkGood", False),
    "mark_bad": ("HistoryMarkBad", False),
    "mark_success": ("HistoryMarkSuccess", False),
    "set_name": ("HistorySetName", True),
    "set_category": ("HistorySetCategory", True),
    "set_parameter": ("HistorySetParameter", True),
    "set_dupe_key": ("HistorySetDupeKey", True),
    "set_dupe_score": ("HistorySetDupeScore", True),
    "set_dupe_mode": ("HistorySetDupeMode", True),
}

DownloadAction = Literal[tuple(DOWNLOAD_ACTIONS)]  # type: ignore[valid-type]
HistoryAction = Literal[tuple(HISTORY_ACTIONS)]  # type: ignore[valid-type]

PAUSE_TARGETS = {
    "download": ("pausedownload", "resumedownload"),
    "post_processing": ("pausepost", "resumepost"),
    "scan": ("pausescan", "resumescan"),
}

SECRET_HINTS = ("password", "secret", "apikey", "api_key", "token")


def _clean(value: Any) -> Any:
    """Merge 64-bit field pairs and drop deprecated fields."""
    return strip_deprecated(merge_hi_lo(value))


def _redact(name: str, value: str) -> str:
    lowered = name.lower()
    if value and any(hint in lowered for hint in SECRET_HINTS):
        return "***"
    return value


def build_server(
    settings: Settings | None = None,
    client: NzbGetClient | None = None,
    search: ReleaseSearch | None = None,
) -> FastMCP:
    """Create the MCP server, wiring tools to NZBGet and the search indexers."""
    settings = settings or load_settings()
    client = client or NzbGetClient(settings)

    if search is None and settings.search_enabled:
        search = ReleaseSearch(
            indexers=[
                Indexer(
                    name=i.name, url=i.url, api_key=i.api_key, categories=i.categories
                )
                for i in settings.indexers
            ],
            timeout=settings.search_timeout,
            cache_ttl=settings.search_cache_ttl,
        )

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield
        finally:
            await client.aclose()
            if search is not None:
                await search.aclose()

    mcp = FastMCP(
        name="nzbget",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        website_url="https://nzbget.com/documentation/api/",
        lifespan=lifespan,
    )

    def _preset(provider: str | None) -> dict[str, Any]:
        if not provider:
            return {}
        key = provider.strip().lower()
        if key not in PROVIDER_PRESETS:
            raise ToolError(
                f"Unknown provider {provider!r}. Known: {', '.join(PROVIDER_PRESETS)}. "
                "Pass host/port explicitly for anything else."
            )
        return PROVIDER_PRESETS[key]

    async def _load_config() -> dict[str, str]:
        """The on-disk config as a dict, the base for any config edit."""
        return {o["Name"]: o["Value"] for o in await call("loadconfig")}

    async def call(method: str, *params: Any) -> Any:
        """Invoke NZBGet, surfacing failures as clean tool errors."""
        try:
            return await client.call(method, *params)
        except NzbGetError as exc:
            raise ToolError(str(exc)) from exc

    # ------------------------------------------------------------------
    # Release search
    # ------------------------------------------------------------------

    if search is not None:

        @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
        async def search_releases(
            query: Annotated[
                str,
                Field(
                    description=(
                        "Space-separated keywords. Drop filler words. Use "
                        "'show sXXeYY' for one episode, 'show sXX' for a season. "
                        "Only add quality tags like 1080p or x265 if asked for."
                    )
                ),
            ],
            categories: Annotated[
                list[str] | None,
                Field(
                    description=(
                        "Narrow by type: movies, tv, anime, music, books, pc, "
                        "console, xxx, other — or raw Newznab category ids."
                    )
                ),
            ] = None,
            indexers: Annotated[
                list[str] | None,
                Field(
                    description=(
                        "Restrict to these indexers. Omit to search all configured "
                        "ones; see the search://indexers resource for the list."
                    )
                ),
            ] = None,
            limit: Annotated[
                int, Field(ge=1, le=100, description="Max results to return.")
            ] = 20,
            include_urls: Annotated[
                bool,
                Field(
                    description=(
                        "Include raw NZB URLs. Off by default because they embed "
                        "API keys and add_nzb only needs the result_id."
                    )
                ),
            ] = False,
        ) -> dict[str, Any]:
            """Search configured Newznab indexers in parallel, merged and ranked.

            Results are deduplicated across indexers by release name and ranked
            by grabs, then recency. Pass a result's `result_id` to `add_nzb` to
            download it.
            """
            try:
                category_ids = resolve_categories(categories)
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            try:
                results, report = await search.search(
                    query, indexers, category_ids, limit=max(limit, 50)
                )
            except UnknownIndexerError as exc:
                raise ToolError(str(exc)) from exc

            shown = results[:limit]
            failed = [name for name, info in report.items() if not info["ok"]]
            payload: dict[str, Any] = {
                "query": query,
                "total": len(results),
                "returned": len(shown),
                "indexers_searched": [n for n, i in report.items() if i["ok"]],
                "results": [r.to_dict(include_url=include_urls) for r in shown],
            }
            if failed:
                # Surfaced rather than hidden: a down or rate-limited indexer
                # changes how much an empty result set should be trusted.
                payload["indexers_failed"] = {n: report[n]["error"] for n in failed}
            return payload

        @mcp.resource("search://indexers", mime_type="application/json")
        def indexers_resource() -> list[dict[str, Any]]:
            """The Newznab indexers this server can search."""
            return search.available_indexers()

    # ------------------------------------------------------------------
    # Read-only tools
    # ------------------------------------------------------------------

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_status(
        verbose: Annotated[
            bool, Field(description="Return every raw field instead of the summary.")
        ] = False,
    ) -> dict[str, Any]:
        """Current NZBGet status: speed, queue size, pause state, disk space, uptime."""
        status = _clean(await call("status"))
        return status if verbose else summarize_status(status)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_queue(
        limit: Annotated[int, Field(ge=1, le=500, description="Max downloads to return.")] = 50,
        status_filter: Annotated[
            str | None,
            Field(
                description=(
                    "Only return downloads whose status matches, e.g. DOWNLOADING, "
                    "QUEUED, PAUSED, UNPACKING. Case-insensitive."
                )
            ),
        ] = None,
        verbose: Annotated[
            bool, Field(description="Return every raw field instead of the summary.")
        ] = False,
    ) -> dict[str, Any]:
        """List downloads currently in the queue, including post-processing items."""
        groups = _clean(await call("listgroups", 0))
        if status_filter:
            wanted = status_filter.strip().upper()
            groups = [g for g in groups if str(g.get("Status", "")).upper() == wanted]
        shown = groups[:limit]
        return {
            "total": len(groups),
            "returned": len(shown),
            "downloads": shown if verbose else [summarize_group(g) for g in shown],
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def list_files(
        nzb_id: Annotated[
            int, Field(description="NZBID of the download; 0 lists files of every download.")
        ] = 0,
        limit: Annotated[int, Field(ge=1, le=1000, description="Max files to return.")] = 100,
        verbose: Annotated[
            bool, Field(description="Return every raw field instead of the summary.")
        ] = False,
    ) -> dict[str, Any]:
        """List the individual files belonging to a queued download."""
        files = _clean(await call("listfiles", 0, 0, nzb_id))
        shown = files[:limit]
        return {
            "total": len(files),
            "returned": len(shown),
            "files": shown if verbose else [summarize_file(f) for f in shown],
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_history(
        limit: Annotated[int, Field(ge=1, le=500, description="Max entries to return.")] = 25,
        status_filter: Annotated[
            str | None,
            Field(
                description=(
                    "Only return entries whose status starts with this, e.g. SUCCESS "
                    "or FAILURE. Case-insensitive."
                )
            ),
        ] = None,
        include_hidden: Annotated[
            bool, Field(description="Include hidden duplicate records (Kind=DUP).")
        ] = False,
        verbose: Annotated[
            bool, Field(description="Return every raw field instead of the summary.")
        ] = False,
    ) -> dict[str, Any]:
        """List finished downloads, newest first, with their success/failure status."""
        items = _clean(await call("history", include_hidden))
        if status_filter:
            wanted = status_filter.strip().upper()
            items = [i for i in items if str(i.get("Status", "")).upper().startswith(wanted)]
        shown = items[:limit]
        return {
            "total": len(items),
            "returned": len(shown),
            "history": shown if verbose else [summarize_history(i) for i in shown],
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_log(
        count: Annotated[int, Field(ge=1, le=500, description="Number of entries.")] = 50,
        nzb_id: Annotated[
            int | None,
            Field(description="Return the log of this download instead of the server log."),
        ] = None,
    ) -> dict[str, Any]:
        """Read the most recent server log entries, or the log of one download."""
        if nzb_id:
            entries = await call("loadlog", nzb_id, 0, count)
        else:
            entries = await call("log", 0, count)
        return {"entries": [summarize_log(e) for e in entries]}

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_version() -> dict[str, Any]:
        """Report the NZBGet version this server is talking to."""
        return {"version": await call("version"), "url": settings.base_url}

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_server_volumes(
        verbose: Annotated[
            bool,
            Field(description="Include the per-second/minute/hour/day volume arrays."),
        ] = False,
    ) -> dict[str, Any]:
        """Downloaded volume per news server. Entry with server_id 0 is the total."""
        volumes = _clean(await call("servervolumes"))
        if verbose:
            return {"servers": volumes}
        return {
            "servers": [
                {
                    "server_id": v.get("ServerID"),
                    "total_bytes": v.get("TotalSize"),
                    "custom_bytes": v.get("CustomSize"),
                }
                for v in volumes
            ]
        }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_config(
        name_filter: Annotated[
            str | None,
            Field(description="Case-insensitive substring match on the option name."),
        ] = None,
        from_disk: Annotated[
            bool,
            Field(description="Read the config file on disk instead of the loaded config."),
        ] = False,
    ) -> dict[str, Any]:
        """Read NZBGet configuration options. Password-like values are redacted."""
        options = await call("loadconfig" if from_disk else "config")
        result = {}
        for option in options:
            name = option.get("Name", "")
            if name_filter and name_filter.lower() not in name.lower():
                continue
            result[name] = _redact(name, option.get("Value", ""))
        return {"count": len(result), "options": result}

    @mcp.tool(annotations={"readOnlyHint": True})
    async def get_system_info() -> dict[str, Any]:
        """Host OS, CPU, disk and library information (needs NZBGet 24.2+)."""
        return _clean(await call("sysinfo"))

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True})
    async def test_news_server(
        username: Annotated[
            str | None,
            Field(description="Provider username. Omit to test the saved server instead."),
        ] = None,
        password: Annotated[
            str | None, Field(description="Provider password.")
        ] = None,
        provider: Annotated[
            str | None,
            Field(description=f"Preset connection settings: {', '.join(PROVIDER_PRESETS)}."),
        ] = None,
        host: Annotated[str | None, Field(description="Hostname, if not using a preset.")] = None,
        port: Annotated[int | None, Field(ge=1, le=65535, description="Port.")] = None,
        encryption: Annotated[bool | None, Field(description="Use TLS/SSL.")] = None,
        slot: Annotated[
            int,
            Field(ge=1, le=99, description="Which saved server to test when no credentials given."),
        ] = 1,
        timeout: Annotated[int, Field(ge=1, le=120, description="Connect timeout, seconds.")] = 20,
    ) -> dict[str, Any]:
        """Test a news server connection, either saved or with supplied credentials.

        Verifies DNS, TLS and login, so it distinguishes a wrong password from
        an unreachable host. Returns ok=false with the provider's own message
        when the connection fails.
        """
        preset = _preset(provider)
        if username is None and password is None and host is None and provider is None:
            saved = await _load_config()
            prefix = f"Server{slot}."
            if not saved.get(f"{prefix}Host"):
                raise ToolError(f"No news server configured in slot {slot}.")
            host = saved[f"{prefix}Host"]
            port = int(saved.get(f"{prefix}Port") or 563)
            username = saved.get(f"{prefix}Username", "")
            password = saved.get(f"{prefix}Password", "")
            encryption = (saved.get(f"{prefix}Encryption", "yes") or "yes").lower() == "yes"

        host = host or preset.get("host")
        if not host:
            raise ToolError("Give a host, or a provider preset, or omit everything to test the saved server.")
        port = port or preset.get("port", 563)
        encryption = preset.get("encryption", True) if encryption is None else encryption

        # NZBGet returns an empty string on success and the error text otherwise.
        message = (await call(
            "testserver", host, port, username or "", password or "",
            bool(encryption), "", timeout, CERT_LEVELS["strict"],
        ) or "").strip()

        return {
            "ok": not message,
            "host": host,
            "port": port,
            "encryption": bool(encryption),
            "message": message or "Connection successful",
        }

    @mcp.resource("nzbget://status", mime_type="application/json")
    async def status_resource() -> dict[str, Any]:
        """Live NZBGet status summary."""
        return summarize_status(_clean(await call("status")))

    @mcp.resource("nzbget://queue", mime_type="application/json")
    async def queue_resource() -> list[dict[str, Any]]:
        """Current download queue."""
        return [summarize_group(g) for g in _clean(await call("listgroups", 0))]

    @mcp.resource("nzbget://history", mime_type="application/json")
    async def history_resource() -> list[dict[str, Any]]:
        """Recent download history (latest 50 entries)."""
        items = _clean(await call("history", False))
        return [summarize_history(i) for i in items[:50]]

    if settings.read_only:
        return mcp

    # ------------------------------------------------------------------
    # Mutating tools
    # ------------------------------------------------------------------

    @mcp.tool
    async def add_nzb(
        result_id: Annotated[
            str | None,
            Field(
                description=(
                    "result_id from a search_releases result — the normal way to "
                    "download something that was just searched for."
                )
            ),
        ] = None,
        url: Annotated[
            str | None,
            Field(description="URL of an NZB to fetch. Use this or file_path or content_base64."),
        ] = None,
        file_path: Annotated[
            str | None,
            Field(description="Path to an .nzb file readable by this MCP server."),
        ] = None,
        content_base64: Annotated[
            str | None, Field(description="Base64-encoded contents of an .nzb file.")
        ] = None,
        filename: Annotated[
            str | None,
            Field(
                description=(
                    "Name with extension (e.g. show.nzb). Required with content_base64; "
                    "derived from file_path or the HTTP headers otherwise."
                )
            ),
        ] = None,
        category: Annotated[str, Field(description="NZBGet category, e.g. movies.")] = "",
        priority: Annotated[
            int,
            Field(
                description=(
                    "-100 very low, -50 low, 0 normal, 50 high, 100 very high, "
                    "900 force (downloads even while paused)."
                )
            ),
        ] = 0,
        add_to_top: Annotated[bool, Field(description="Queue at the top instead of the end.")] = False,
        add_paused: Annotated[bool, Field(description="Add in paused state.")] = False,
        auto_category: Annotated[
            bool, Field(description="Let NZBGet detect the category from the NZB.")
        ] = True,
        dupe_key: Annotated[str, Field(description="Duplicate key for dupe checking.")] = "",
        dupe_score: Annotated[int, Field(description="Duplicate score.")] = 0,
        dupe_mode: Annotated[Literal["SCORE", "ALL", "FORCE"], Field(description="Duplicate mode.")] = "SCORE",
        post_processing_params: Annotated[
            dict[str, str] | None,
            Field(description='Post-processing parameters, e.g. {"*unpack:": "yes"}.'),
        ] = None,
    ) -> dict[str, Any]:
        """Add an NZB to the download queue by search result, URL, file, or content."""
        sources = [s for s in (result_id, url, file_path, content_base64) if s]
        if len(sources) != 1:
            raise ToolError(
                "Provide exactly one of result_id, url, file_path, or content_base64."
            )

        if result_id:
            if search is None:
                raise ToolError("Search is not configured, so result_id cannot be resolved.")
            hit = search.cache.get(result_id)
            if hit is None:
                raise ToolError(
                    f"Unknown or expired result_id {result_id!r}. "
                    "Run search_releases again to refresh the results."
                )
            url = hit.nzb_url
            # Naming the item after the release keeps the queue readable; the
            # extension tells NZBGet how to handle the fetched body.
            filename = filename or f"{hit.title}.nzb"

        if url:
            content = url
            name = filename or ""
        elif file_path:
            path = Path(file_path).expanduser()
            if not path.is_file():
                raise ToolError(f"No such file: {path}")
            content = base64.b64encode(path.read_bytes()).decode("ascii")
            name = filename or path.name
        else:
            content = content_base64 or ""
            name = filename or ""
            if not name:
                raise ToolError("filename is required when passing content_base64.")

        pp_params = [
            {"Name": key, "Value": value}
            for key, value in (post_processing_params or {}).items()
        ]

        nzb_id = await call(
            "append",
            name,
            content,
            category,
            priority,
            add_to_top,
            add_paused,
            dupe_key,
            dupe_score,
            dupe_mode,
            auto_category,
            pp_params,
        )
        if not isinstance(nzb_id, int) or nzb_id <= 0:
            raise ToolError(
                f"NZBGet refused the download (error code {nzb_id}). "
                "Check the filename extension, the URL, and the category name."
            )
        return {"nzb_id": nzb_id, "name": name or url}

    @mcp.tool
    async def manage_downloads(
        action: Annotated[DownloadAction, Field(description="What to do with the downloads.")],
        nzb_ids: Annotated[
            list[int],
            Field(description="NZBIDs from list_queue. Empty is only valid for 'sort' (sorts all)."),
        ],
        param: Annotated[
            str | None,
            Field(
                description=(
                    "Value for actions that need one: move_offset (integer), "
                    "set_priority (integer), set_category/apply_category/set_name (text), "
                    "set_parameter (Name=Value), set_dupe_mode (SCORE|ALL|FORCE), "
                    "sort (name|priority|category|size|left, optionally suffixed + or -)."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Act on queued downloads: pause, resume, delete, reorder, recategorise, sort."""
        command, needs_param = DOWNLOAD_ACTIONS[action]
        if needs_param and not param:
            raise ToolError(f"Action '{action}' requires a `param` value.")
        if not nzb_ids and action != "sort":
            raise ToolError(f"Action '{action}' requires at least one nzb_id.")
        return await _edit(command, param or "", nzb_ids, action)

    @mcp.tool
    async def manage_history(
        action: Annotated[HistoryAction, Field(description="What to do with the history items.")],
        nzb_ids: Annotated[list[int], Field(description="NZBIDs from get_history.")],
        param: Annotated[
            str | None,
            Field(description="Value for set_name, set_category, set_parameter, set_dupe_* actions."),
        ] = None,
    ) -> dict[str, Any]:
        """Act on history items: requeue, redownload, re-post-process, mark, delete."""
        command, needs_param = HISTORY_ACTIONS[action]
        if needs_param and not param:
            raise ToolError(f"Action '{action}' requires a `param` value.")
        if not nzb_ids:
            raise ToolError(f"Action '{action}' requires at least one nzb_id.")
        return await _edit(command, param or "", nzb_ids, action)

    async def _edit(command: str, param: str, ids: list[int], action: str) -> dict[str, Any]:
        try:
            ok = await client.edit_queue(command, param, ids)
        except NzbGetError as exc:
            raise ToolError(str(exc)) from exc
        if not ok:
            raise ToolError(
                f"NZBGet rejected '{action}' for {ids or 'all items'}. "
                "The IDs may no longer exist or the parameter may be invalid."
            )
        return {"action": action, "command": command, "nzb_ids": ids, "success": True}

    @mcp.tool
    async def set_paused(
        paused: Annotated[bool, Field(description="True to pause, False to resume.")],
        target: Annotated[
            Literal["download", "post_processing", "scan", "all"],
            Field(description="Which activity to pause or resume."),
        ] = "download",
    ) -> dict[str, Any]:
        """Pause or resume downloading, post-processing, or scanning of the incoming folder."""
        targets = list(PAUSE_TARGETS) if target == "all" else [target]
        for name in targets:
            pause_method, resume_method = PAUSE_TARGETS[name]
            await call(pause_method if paused else resume_method)
        return {"target": target, "paused": paused}

    @mcp.tool
    async def schedule_resume(
        seconds: Annotated[
            int, Field(ge=0, description="Seconds to wait; 0 cancels a pending scheduled resume.")
        ],
    ) -> dict[str, Any]:
        """Resume all paused activities automatically after a delay."""
        await call("scheduleresume", seconds)
        return {"resume_in_seconds": seconds}

    @mcp.tool
    async def set_speed_limit(
        kilobytes_per_second: Annotated[
            int, Field(ge=0, description="Speed limit in KB/s. 0 removes the limit.")
        ],
    ) -> dict[str, Any]:
        """Set the download speed limit in kilobytes per second."""
        ok = await call("rate", kilobytes_per_second)
        if not ok:
            raise ToolError(f"NZBGet rejected the speed limit {kilobytes_per_second} KB/s.")
        return {"speed_limit_kb_per_sec": kilobytes_per_second}

    @mcp.tool
    async def scan_incoming(
        wait: Annotated[bool, Field(description="Wait for the scan to finish before returning.")] = True,
    ) -> dict[str, Any]:
        """Rescan NZBGet's incoming directory (NzbDir) for new .nzb files."""
        await call("scan", wait)
        return {"scanned": True, "waited": wait}

    @mcp.tool
    async def write_log(
        kind: Annotated[
            Literal["INFO", "WARNING", "ERROR", "DETAIL", "DEBUG"],
            Field(description="Severity of the entry."),
        ],
        text: Annotated[str, Field(description="Message to append to NZBGet's log.")],
    ) -> dict[str, Any]:
        """Append a message to NZBGet's log."""
        await call("writelog", kind, text)
        return {"kind": kind, "text": text}

    @mcp.tool(annotations={"destructiveHint": True})
    async def configure_news_server(
        username: Annotated[str, Field(description="Provider username.")],
        password: Annotated[str, Field(description="Provider password.")],
        provider: Annotated[
            str | None,
            Field(
                description=(
                    f"Preset connection settings: {', '.join(PROVIDER_PRESETS)}. "
                    "Omit and pass host/port for any other provider."
                )
            ),
        ] = None,
        host: Annotated[str | None, Field(description="Hostname, if not using a preset.")] = None,
        port: Annotated[int | None, Field(ge=1, le=65535, description="Port. 563 for TLS.")] = None,
        encryption: Annotated[bool | None, Field(description="Use TLS/SSL.")] = None,
        connections: Annotated[
            int | None,
            Field(
                ge=1,
                le=200,
                description=(
                    "Simultaneous connections. Use what your plan allows — more "
                    "than that gets refused by the provider."
                ),
            ),
        ] = None,
        name: Annotated[str | None, Field(description="Display name for the server.")] = None,
        slot: Annotated[
            int, Field(ge=1, le=99, description="Server slot; 1 unless adding a backup provider.")
        ] = 1,
        level: Annotated[
            int,
            Field(
                ge=0,
                le=99,
                description="0 for a main server; higher levels are fallbacks tried in order.",
            ),
        ] = 0,
        verify: Annotated[
            bool, Field(description="Test the connection first and refuse to save if it fails.")
        ] = True,
        reload_after: Annotated[
            bool, Field(description="Reload NZBGet so the change takes effect immediately.")
        ] = True,
    ) -> dict[str, Any]:
        """Save a usenet provider into NZBGet's news server settings.

        Reads the whole config, changes only the Server<slot>.* keys and writes
        it back, so unrelated settings are preserved. By default the credentials
        are tested before anything is saved.
        """
        preset = _preset(provider)
        host = host or preset.get("host")
        if not host:
            raise ToolError("Give a host, or a provider preset to take one from.")
        port = port or preset.get("port", 563)
        encryption = preset.get("encryption", True) if encryption is None else encryption
        connections = connections or preset.get("connections", 8)
        name = name or (provider or host)

        if verify:
            message = (await call(
                "testserver", host, port, username, password,
                bool(encryption), "", 20, CERT_LEVELS["strict"],
            ) or "").strip()
            if message:
                raise ToolError(
                    f"Not saved — the connection test failed: {message}. "
                    "Pass verify=false to save anyway."
                )

        options = await _load_config()
        prefix = f"Server{slot}."
        options.update({
            f"{prefix}Active": "yes",
            f"{prefix}Name": name,
            f"{prefix}Level": str(level),
            f"{prefix}Host": host,
            f"{prefix}Port": str(port),
            f"{prefix}Username": username,
            f"{prefix}Password": password,
            f"{prefix}Encryption": "yes" if encryption else "no",
            f"{prefix}Connections": str(connections),
            f"{prefix}CertVerification": "strict" if encryption else "none",
        })

        saved = await call(
            "saveconfig", [{"Name": k, "Value": v} for k, v in options.items()]
        )
        if not saved:
            raise ToolError("NZBGet refused to save the configuration.")

        if reload_after:
            await call("reload")

        return {
            "slot": slot,
            "name": name,
            "host": host,
            "port": port,
            "encryption": bool(encryption),
            "connections": connections,
            "verified": verify,
            "reloaded": reload_after,
        }

    @mcp.tool(annotations={"destructiveHint": True})
    async def reload_server() -> dict[str, Any]:
        """Reload NZBGet, re-reading its configuration file. Interrupts active downloads."""
        await call("reload")
        return {"reloaded": True}

    if settings.allow_shutdown:

        @mcp.tool(annotations={"destructiveHint": True})
        async def shutdown_server() -> dict[str, Any]:
            """Shut NZBGet down. It will not restart unless something else restarts it."""
            await call("shutdown")
            return {"shutdown": True}

    return mcp


def run() -> None:
    """Entry point: build the server and serve it over the configured transport."""
    settings = load_settings()
    mcp = build_server(settings)
    if settings.transport == "stdio":
        # The banner would otherwise share stdout with the JSON-RPC stream.
        mcp.run(transport="stdio", show_banner=False)
    else:
        mcp.run(
            transport=settings.transport,
            host=settings.host,
            port=settings.port,
            path=settings.path,
            log_level=os.getenv("LOG_LEVEL", "info").lower(),
        )
