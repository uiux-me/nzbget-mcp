"""End-to-end tool calls against a fake NZBGet, driven through an MCP client."""

from __future__ import annotations

import base64

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError


STATUS = {
    "RemainingSizeLo": 500,
    "RemainingSizeHi": 0,
    "DownloadRateLo": 1_048_576,
    "DownloadRateHi": 0,
    "DownloadPaused": False,
    "FreeDiskSpaceLo": 0,
    "FreeDiskSpaceHi": 100,
    "ServerTime": 1700000000,
}

GROUPS = [
    {
        "NZBID": 11,
        "NZBName": "Movie.2024",
        "Kind": "NZB",
        "Status": "DOWNLOADING",
        "Category": "movies",
        "MaxPriority": 0,
        "FileSizeLo": 2000,
        "FileSizeHi": 0,
        "RemainingSizeLo": 500,
        "RemainingSizeHi": 0,
        "Health": 1000,
    },
    {
        "NZBID": 12,
        "NZBName": "Show.S01E02",
        "Kind": "NZB",
        "Status": "PAUSED",
        "FileSizeLo": 100,
        "FileSizeHi": 0,
        "RemainingSizeLo": 100,
        "RemainingSizeHi": 0,
    },
]


async def test_get_status_returns_merged_64bit_values(fake, build):
    fake.results["status"] = STATUS
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("get_status")).data
    assert data["download_rate_bytes_per_sec"] == 1_048_576
    assert data["free_disk_space_bytes"] == 100 * 2**32
    assert data["server_time"].startswith("2023-11-14T")


async def test_get_status_verbose_returns_raw_struct(fake, build):
    fake.results["status"] = STATUS
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("get_status", {"verbose": True})).data
    assert data["RemainingSize"] == 500
    assert "RemainingSizeLo" not in data


async def test_list_queue_summarizes_and_filters(fake, build):
    fake.results["listgroups"] = GROUPS
    async with Client(build()) as mcp:
        everything = (await mcp.call_tool("list_queue")).data
        paused = (await mcp.call_tool("list_queue", {"status_filter": "paused"})).data

    assert everything["total"] == 2
    first = everything["downloads"][0]
    assert first["nzb_id"] == 11 and first["progress_percent"] == 75.0
    assert paused["total"] == 1 and paused["downloads"][0]["nzb_id"] == 12
    assert fake.params_for("listgroups") == [0]


async def test_list_queue_limit_reports_truncation(fake, build):
    fake.results["listgroups"] = GROUPS
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("list_queue", {"limit": 1})).data
    assert (data["total"], data["returned"]) == (2, 1)


async def test_get_history_filters_by_status_prefix(fake, build):
    fake.results["history"] = [
        {"NZBID": 1, "Name": "ok", "Status": "SUCCESS/ALL", "HistoryTime": 1700000000},
        {"NZBID": 2, "Name": "bad", "Status": "FAILURE/UNPACK"},
    ]
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("get_history", {"status_filter": "failure"})).data
    assert data["returned"] == 1
    assert data["history"][0]["name"] == "bad"
    assert fake.params_for("history") == [False]


async def test_get_config_redacts_secrets(fake, build):
    fake.results["config"] = [
        {"Name": "ControlPassword", "Value": "hunter2"},
        {"Name": "MainDir", "Value": "/downloads"},
    ]
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("get_config")).data
    assert data["options"] == {"ControlPassword": "***", "MainDir": "/downloads"}


async def test_get_log_reads_download_log_when_given_an_id(fake, build):
    fake.results["loadlog"] = [{"ID": 1, "Kind": "INFO", "Time": 1700000000, "Text": "hi"}]
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("get_log", {"nzb_id": 5, "count": 10})).data
    assert fake.params_for("loadlog") == [5, 0, 10]
    assert data["entries"][0]["text"] == "hi"


async def test_add_nzb_by_url_sends_documented_parameter_order(fake, build):
    fake.results["append"] = 42
    async with Client(build()) as mcp:
        data = (
            await mcp.call_tool(
                "add_nzb",
                {"url": "http://indexer/x.nzb", "category": "movies", "priority": 50},
            )
        ).data
    assert data["nzb_id"] == 42
    assert fake.params_for("append") == [
        "", "http://indexer/x.nzb", "movies", 50, False, False, "", 0, "SCORE", True, [],
    ]


async def test_add_nzb_from_file_is_base64_encoded(fake, build, tmp_path):
    nzb = tmp_path / "grab.nzb"
    nzb.write_bytes(b"<nzb/>")
    fake.results["append"] = 7
    async with Client(build()) as mcp:
        await mcp.call_tool("add_nzb", {"file_path": str(nzb)})
    params = fake.params_for("append")
    assert params[0] == "grab.nzb"
    assert base64.b64decode(params[1]) == b"<nzb/>"


async def test_add_nzb_passes_post_processing_params(fake, build):
    fake.results["append"] = 1
    async with Client(build()) as mcp:
        await mcp.call_tool(
            "add_nzb",
            {"url": "http://x/y.nzb", "post_processing_params": {"*unpack:": "yes"}},
        )
    assert fake.params_for("append")[10] == [{"Name": "*unpack:", "Value": "yes"}]


async def test_add_nzb_rejects_ambiguous_sources(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="exactly one"):
            await mcp.call_tool("add_nzb", {"url": "http://x/y.nzb", "content_base64": "abc"})


async def test_add_nzb_surfaces_nzbget_error_code(fake, build):
    fake.results["append"] = 0
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="refused"):
            await mcp.call_tool("add_nzb", {"url": "http://x/y.nzb"})


async def test_manage_downloads_uses_v18_editqueue_signature(fake, build):
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("manage_downloads", {"action": "pause", "nzb_ids": [11]})).data
    assert data["command"] == "GroupPause"
    assert fake.params_for("editqueue") == ["GroupPause", "", [11]]


async def test_manage_downloads_falls_back_to_pre_v18_signature(fake, build):
    fake.results["version"] = "17.1"
    async with Client(build()) as mcp:
        await mcp.call_tool("manage_downloads", {"action": "move_offset", "nzb_ids": [11], "param": "-2"})
    assert fake.params_for("editqueue") == ["GroupMoveOffset", -2, "", [11]]


async def test_manage_downloads_requires_param_where_documented(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="requires a `param`"):
            await mcp.call_tool("manage_downloads", {"action": "set_category", "nzb_ids": [11]})


async def test_manage_downloads_allows_empty_ids_only_for_sort(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool("manage_downloads", {"action": "sort", "nzb_ids": [], "param": "size-"})
        with pytest.raises(ToolError, match="at least one nzb_id"):
            await mcp.call_tool("manage_downloads", {"action": "delete", "nzb_ids": []})
    assert fake.params_for("editqueue") == ["GroupSort", "size-", []]


async def test_manage_downloads_reports_rejection(fake, build):
    fake.results["editqueue"] = False
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="rejected 'delete'"):
            await mcp.call_tool("manage_downloads", {"action": "delete", "nzb_ids": [99]})


async def test_manage_history_maps_to_history_commands(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool("manage_history", {"action": "return_to_queue", "nzb_ids": [3]})
    assert fake.params_for("editqueue") == ["HistoryReturn", "", [3]]


async def test_set_paused_all_hits_every_pause_method(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool("set_paused", {"paused": True, "target": "all"})
    assert [name for name, _ in fake.calls] == ["pausedownload", "pausepost", "pausescan"]


async def test_set_paused_resumes(fake, build):
    async with Client(build()) as mcp:
        await mcp.call_tool("set_paused", {"paused": False, "target": "download"})
    assert fake.calls == [("resumedownload", [])]


async def test_set_speed_limit_reports_rejection(fake, build):
    fake.results["rate"] = False
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="rejected the speed limit"):
            await mcp.call_tool("set_speed_limit", {"kilobytes_per_second": 999999})


async def test_read_only_mode_hides_mutating_tools(fake, build):
    async with Client(build(read_only=True)) as mcp:
        names = {tool.name for tool in await mcp.list_tools()}
    assert "get_status" in names
    assert names.isdisjoint({"add_nzb", "manage_downloads", "set_paused", "reload_server"})


async def test_shutdown_tool_is_opt_in(fake, build):
    async with Client(build()) as mcp:
        assert "shutdown_server" not in {t.name for t in await mcp.list_tools()}
    async with Client(build(allow_shutdown=True)) as mcp:
        assert "shutdown_server" in {t.name for t in await mcp.list_tools()}


async def test_auth_failure_is_reported_clearly(fake, build):
    fake.status_code = 401
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="NZBGET_USERNAME"):
            await mcp.call_tool("get_status")


async def test_rpc_error_is_reported_clearly(fake, build):
    fake.error = {"code": -32601, "message": "Invalid procedure"}
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="Invalid procedure"):
            await mcp.call_tool("get_status")


async def test_resources_expose_the_same_summaries(fake, build):
    fake.results["listgroups"] = GROUPS
    async with Client(build()) as mcp:
        contents = await mcp.read_resource("nzbget://queue")
    assert '"nzb_id": 11' in contents[0].text


# --- release search ----------------------------------------------------------


async def test_search_tools_are_absent_without_configured_indexers(fake, build):
    async with Client(build()) as mcp:
        names = {t.name for t in await mcp.list_tools()}
        assert "search_releases" not in names
        assert "search://indexers" not in {str(r.uri) for r in await mcp.list_resources()}


async def test_search_releases_returns_ranked_results(fake, build, indexers):
    indexers.bodies["geek.test"] = indexers.feed(
        indexers.item("Show.S01E01.1080p", grabs=5),
        indexers.item("Show.S01E01.2160p", grabs=99),
    )
    async with Client(build(search_kwargs={})) as mcp:
        data = (await mcp.call_tool("search_releases", {"query": "show s01e01"})).data

    assert data["total"] == 2
    assert [r["title"] for r in data["results"]] == ["Show.S01E01.2160p", "Show.S01E01.1080p"]
    assert data["indexers_searched"] == ["geek"]
    # URLs embed API keys, so they stay out unless asked for.
    assert "nzb_url" not in data["results"][0]
    assert data["results"][0]["size_human"] == "1.50 GiB"


async def test_search_releases_can_include_urls(fake, build, indexers):
    indexers.bodies["geek.test"] = indexers.feed(indexers.item("Thing"))
    async with Client(build(search_kwargs={})) as mcp:
        data = (await mcp.call_tool(
            "search_releases", {"query": "thing", "include_urls": True}
        )).data
    assert data["results"][0]["nzb_url"].endswith(".nzb")


async def test_search_releases_reports_a_failed_indexer(fake, build, indexers):
    indexers.status["geek.test"] = 500
    async with Client(build(search_kwargs={})) as mcp:
        data = (await mcp.call_tool("search_releases", {"query": "x"})).data
    assert data["total"] == 0
    assert "geek" in data["indexers_failed"]


async def test_search_releases_rejects_unknown_categories(fake, build, indexers):
    async with Client(build(search_kwargs={})) as mcp:
        with pytest.raises(ToolError, match="Known:"):
            await mcp.call_tool("search_releases", {"query": "x", "categories": ["telly"]})


async def test_add_nzb_by_result_id_uses_the_cached_url(fake, build, indexers):
    indexers.bodies["geek.test"] = indexers.feed(indexers.item("Show.S01E01", grabs=7))
    fake.results["append"] = 55
    async with Client(build(search_kwargs={})) as mcp:
        found = (await mcp.call_tool("search_releases", {"query": "show"})).data
        result_id = found["results"][0]["result_id"]
        added = (await mcp.call_tool("add_nzb", {"result_id": result_id})).data

    assert added["nzb_id"] == 55
    params = fake.params_for("append")
    assert params[0] == "Show.S01E01.nzb"           # named after the release
    assert params[1].endswith(".nzb")                # content is the NZB URL


async def test_add_nzb_rejects_an_unknown_result_id(fake, build, indexers):
    async with Client(build(search_kwargs={})) as mcp:
        with pytest.raises(ToolError, match="Unknown or expired"):
            await mcp.call_tool("add_nzb", {"result_id": "deadbeef"})


async def test_add_nzb_still_refuses_two_sources(fake, build, indexers):
    async with Client(build(search_kwargs={})) as mcp:
        with pytest.raises(ToolError, match="exactly one"):
            await mcp.call_tool("add_nzb", {"result_id": "a", "url": "http://x/y.nzb"})


# --- news server (provider) --------------------------------------------------

CONFIG = [
    {"Name": "MainDir", "Value": "/downloads"},
    {"Name": "Server1.Host", "Value": "my.newsserver.com"},
    {"Name": "Server1.Port", "Value": "563"},
    {"Name": "Server1.Username", "Value": "olduser"},
    {"Name": "Server1.Password", "Value": "oldpass"},
    {"Name": "Server1.Encryption", "Value": "yes"},
    {"Name": "ControlPassword", "Value": "secret"},
]


async def test_test_news_server_treats_empty_string_as_success(fake, build):
    fake.results["testserver"] = ""
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("test_news_server", {
            "username": "u", "password": "p", "provider": "newshosting",
        })).data

    assert data["ok"] is True and data["message"] == "Connection successful"
    assert (data["host"], data["port"], data["encryption"]) == ("news.newshosting.com", 563, True)
    assert fake.params_for("testserver")[:5] == ["news.newshosting.com", 563, "u", "p", True]


async def test_test_news_server_surfaces_the_provider_message(fake, build):
    fake.results["testserver"] = "Authorization for test server failed: 502 Authentication Failed"
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("test_news_server", {
            "username": "u", "password": "bad", "provider": "newshosting",
        })).data
    assert data["ok"] is False and "502" in data["message"]


async def test_test_news_server_falls_back_to_the_saved_server(fake, build):
    fake.results["loadconfig"] = CONFIG
    fake.results["testserver"] = ""
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("test_news_server")).data
    assert data["host"] == "my.newsserver.com"
    assert fake.params_for("testserver")[2:4] == ["olduser", "oldpass"]


async def test_unknown_provider_preset_is_rejected(fake, build):
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="Unknown provider"):
            await mcp.call_tool("test_news_server", {
                "username": "u", "password": "p", "provider": "nope",
            })


async def test_configure_news_server_preserves_unrelated_options(fake, build):
    fake.results["loadconfig"] = CONFIG
    fake.results["testserver"] = ""
    async with Client(build()) as mcp:
        data = (await mcp.call_tool("configure_news_server", {
            "provider": "newshosting", "username": "me", "password": "pw", "connections": 40,
        })).data

    saved = {o["Name"]: o["Value"] for o in fake.params_for("saveconfig")[0]}
    assert saved["Server1.Host"] == "news.newshosting.com"
    assert saved["Server1.Username"] == "me"
    assert saved["Server1.Password"] == "pw"
    assert saved["Server1.Connections"] == "40"
    assert saved["Server1.Encryption"] == "yes"
    assert saved["Server1.CertVerification"] == "strict"
    # Everything else survives the round trip.
    assert saved["MainDir"] == "/downloads"
    assert saved["ControlPassword"] == "secret"
    assert data["reloaded"] is True
    assert "reload" in [name for name, _ in fake.calls]


async def test_configure_news_server_refuses_to_save_a_failing_connection(fake, build):
    fake.results["loadconfig"] = CONFIG
    fake.results["testserver"] = "Could not resolve hostname"
    async with Client(build()) as mcp:
        with pytest.raises(ToolError, match="Not saved"):
            await mcp.call_tool("configure_news_server", {
                "provider": "newshosting", "username": "me", "password": "pw",
            })
    assert "saveconfig" not in [name for name, _ in fake.calls]


async def test_configure_news_server_can_skip_verification(fake, build):
    fake.results["loadconfig"] = CONFIG
    async with Client(build()) as mcp:
        await mcp.call_tool("configure_news_server", {
            "host": "news.example.net", "port": 119, "encryption": False,
            "username": "me", "password": "pw", "verify": False, "slot": 2,
        })
    saved = {o["Name"]: o["Value"] for o in fake.params_for("saveconfig")[0]}
    assert saved["Server2.Host"] == "news.example.net"
    assert saved["Server2.Encryption"] == "no"
    assert saved["Server2.CertVerification"] == "none"
    assert "testserver" not in [name for name, _ in fake.calls]


async def test_configure_news_server_is_hidden_in_read_only_mode(fake, build):
    async with Client(build(read_only=True)) as mcp:
        names = {t.name for t in await mcp.list_tools()}
    assert "configure_news_server" not in names
    assert "test_news_server" in names  # testing a connection changes nothing
