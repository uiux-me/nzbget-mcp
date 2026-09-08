"""Turn NZBGet's wire format into something an LLM can read cheaply.

Two things make the raw API awkward: 64-bit values are split across
``<Name>Lo``/``<Name>Hi`` unsigned 32-bit pairs, and every struct carries
dozens of deprecated or redundant fields. These helpers merge the pairs
and pick out the fields that actually answer a user's question.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# Fields the API itself documents as deprecated; dropped from summaries.
DEPRECATED_FIELDS = frozenset(
    {
        "FirstID",
        "LastID",
        "NZBNicename",
        "MinPriority",
        "ParJobCount",
        "ServerPaused",
        "Download2Paused",
        "Deleted",
    }
)


def merge_hi_lo(value: Any) -> Any:
    """Recursively fold ``<Name>Lo``/``<Name>Hi`` pairs into one integer field."""
    if isinstance(value, list):
        return [merge_hi_lo(item) for item in value]
    if not isinstance(value, dict):
        return value

    merged: dict[str, Any] = {}
    redundant: set[str] = set()
    for key in value:
        if not key.endswith("Lo"):
            continue
        base = key[:-2]
        hi_key = f"{base}Hi"
        if hi_key not in value:
            continue
        lo, hi = value[key], value[hi_key]
        if not isinstance(lo, int) or not isinstance(hi, int):
            continue
        merged[base] = (hi << 32) + lo
        # The MB variant is a lossy copy of the same number.
        redundant.update({key, hi_key, f"{base}MB"})

    result: dict[str, Any] = {}
    for key, raw in value.items():
        if key in redundant:
            continue
        result[key] = merged[key] if key in merged else merge_hi_lo(raw)
    for key, number in merged.items():
        result.setdefault(key, number)
    return result


def to_iso(timestamp: Any) -> str | None:
    """Convert a C/Unix timestamp to an ISO-8601 UTC string."""
    if not isinstance(timestamp, int) or timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _percent(done: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, done / total * 100)), 2)


def summarize_group(group: dict[str, Any]) -> dict[str, Any]:
    """Curated view of one ``listgroups`` entry."""
    total = group.get("FileSize", 0) or 0
    remaining = group.get("RemainingSize", 0) or 0
    summary = {
        "nzb_id": group.get("NZBID"),
        "name": group.get("NZBName"),
        "kind": group.get("Kind"),
        "status": group.get("Status"),
        "category": group.get("Category"),
        "priority": group.get("MaxPriority"),
        "total_bytes": total,
        "remaining_bytes": remaining,
        "paused_bytes": group.get("PausedSize"),
        "progress_percent": _percent(total - remaining, total),
        "active_downloads": group.get("ActiveDownloads"),
        "files_remaining": group.get("RemainingFileCount"),
        "files_total": group.get("FileCount"),
        "health_percent": _health(group.get("Health")),
        "dest_dir": group.get("DestDir"),
    }
    if group.get("Kind") == "URL":
        summary["url"] = group.get("URL")
    if group.get("PostTotalTimeSec"):
        summary["post_processing_seconds"] = group["PostTotalTimeSec"]
    return summary


def summarize_history(item: dict[str, Any]) -> dict[str, Any]:
    """Curated view of one ``history`` entry."""
    return {
        "nzb_id": item.get("NZBID"),
        "name": item.get("Name") or item.get("NZBName"),
        "kind": item.get("Kind"),
        "status": item.get("Status"),
        "category": item.get("Category"),
        "total_bytes": item.get("FileSize"),
        "downloaded_bytes": item.get("DownloadedSize"),
        "finished_at": to_iso(item.get("HistoryTime")),
        "download_seconds": item.get("DownloadTimeSec"),
        "post_processing_seconds": item.get("PostTotalTimeSec"),
        "health_percent": _health(item.get("Health")),
        "par_status": item.get("ParStatus"),
        "unpack_status": item.get("UnpackStatus"),
        "move_status": item.get("MoveStatus"),
        "script_status": item.get("ScriptStatus"),
        "delete_status": item.get("DeleteStatus"),
        "mark_status": item.get("MarkStatus"),
        "dest_dir": item.get("FinalDir") or item.get("DestDir"),
        "can_return_to_queue": bool(item.get("RemainingFileCount")),
    }


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    """Curated view of the ``status`` struct."""
    return {
        "download_rate_bytes_per_sec": status.get("DownloadRate"),
        "average_rate_bytes_per_sec": status.get("AverageDownloadRate"),
        "speed_limit_bytes_per_sec": status.get("DownloadLimit"),
        "remaining_bytes": status.get("RemainingSize"),
        "forced_bytes": status.get("ForcedSize"),
        "downloaded_bytes_since_start": status.get("DownloadedSize"),
        "downloaded_bytes_today": status.get("DaySize"),
        "downloaded_bytes_this_month": status.get("MonthSize"),
        "free_disk_space_bytes": status.get("FreeDiskSpace"),
        "article_cache_bytes": status.get("ArticleCache"),
        "download_paused": status.get("DownloadPaused"),
        "post_paused": status.get("PostPaused"),
        "scan_paused": status.get("ScanPaused"),
        "server_standby": status.get("ServerStandBy"),
        "quota_reached": status.get("QuotaReached"),
        "queued_post_jobs": status.get("PostJobCount"),
        "queued_urls": status.get("UrlCount"),
        "thread_count": status.get("ThreadCount"),
        "uptime_seconds": status.get("UpTimeSec"),
        "download_time_seconds": status.get("DownloadTimeSec"),
        "server_time": to_iso(status.get("ServerTime")),
        "resume_time": to_iso(status.get("ResumeTime")),
        "news_servers": status.get("NewsServers"),
    }


def summarize_file(item: dict[str, Any]) -> dict[str, Any]:
    """Curated view of one ``listfiles`` entry."""
    progress = item.get("Progress")
    return {
        "file_id": item.get("ID"),
        "nzb_id": item.get("NZBID"),
        "filename": item.get("Filename"),
        "filename_confirmed": item.get("FilenameConfirmed"),
        "total_bytes": item.get("FileSize"),
        "remaining_bytes": item.get("RemainingSize"),
        "progress_percent": round(progress / 10, 1) if isinstance(progress, int) else None,
        "paused": item.get("Paused"),
        "active_downloads": item.get("ActiveDownloads"),
        "posted_at": to_iso(item.get("PostTime")),
    }


def summarize_log(entry: dict[str, Any]) -> dict[str, Any]:
    """Curated view of one ``log``/``loadlog`` entry."""
    return {
        "id": entry.get("ID"),
        "kind": entry.get("Kind"),
        "time": to_iso(entry.get("Time")),
        "text": entry.get("Text"),
    }


def _health(permille: Any) -> float | None:
    """NZBGet reports health in permille; 1000 means 100.0%."""
    if not isinstance(permille, int):
        return None
    return round(permille / 10, 1)


def strip_deprecated(value: Any) -> Any:
    """Recursively drop fields the API documents as deprecated."""
    if isinstance(value, list):
        return [strip_deprecated(item) for item in value]
    if isinstance(value, dict):
        return {
            key: strip_deprecated(item)
            for key, item in value.items()
            if key not in DEPRECATED_FIELDS
        }
    return value
