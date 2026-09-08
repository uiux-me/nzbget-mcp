from nzbget_mcp.normalize import merge_hi_lo, summarize_group, summarize_status, to_iso


def test_merge_hi_lo_combines_pairs_and_drops_mb():
    raw = {"FileSizeLo": 1000, "FileSizeHi": 2, "FileSizeMB": 8192, "NZBName": "x"}
    assert merge_hi_lo(raw) == {"FileSize": 2 * 2**32 + 1000, "NZBName": "x"}


def test_merge_hi_lo_overwrites_deprecated_32bit_field():
    raw = {"DownloadRate": 4096, "DownloadRateLo": 5000, "DownloadRateHi": 0}
    assert merge_hi_lo(raw)["DownloadRate"] == 5000


def test_merge_hi_lo_recurses_into_lists_and_nested_structs():
    raw = {"Servers": [{"TotalSizeLo": 5, "TotalSizeHi": 1}], "OS": {"Name": "Linux"}}
    merged = merge_hi_lo(raw)
    assert merged["Servers"][0]["TotalSize"] == 2**32 + 5
    assert merged["OS"] == {"Name": "Linux"}


def test_merge_hi_lo_ignores_unpaired_lo_field():
    raw = {"SomethingLo": 3, "Other": 1}
    assert merge_hi_lo(raw) == {"SomethingLo": 3, "Other": 1}


def test_summarize_group_computes_progress_and_health():
    group = merge_hi_lo(
        {
            "NZBID": 7,
            "NZBName": "Show.S01E01",
            "Kind": "NZB",
            "Status": "DOWNLOADING",
            "FileSizeLo": 1000,
            "FileSizeHi": 0,
            "RemainingSizeLo": 250,
            "RemainingSizeHi": 0,
            "Health": 985,
            "MaxPriority": 50,
        }
    )
    summary = summarize_group(group)
    assert summary["progress_percent"] == 75.0
    assert summary["health_percent"] == 98.5
    assert summary["priority"] == 50
    assert "url" not in summary


def test_summarize_group_handles_zero_size():
    assert summarize_group({"FileSize": 0, "RemainingSize": 0})["progress_percent"] is None


def test_summarize_status_maps_pause_flags():
    status = summarize_status({"DownloadPaused": True, "PostPaused": False, "ServerTime": 0})
    assert status["download_paused"] is True
    assert status["post_paused"] is False
    assert status["server_time"] is None


def test_to_iso():
    assert to_iso(1700000000).startswith("2023-11-14T")
    assert to_iso(0) is None
    assert to_iso(None) is None
