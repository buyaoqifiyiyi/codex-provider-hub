"""Local, metadata-only evidence of actual Jev API request attempts."""
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


OPERATIONS = {"jev_decide", "jev_route_advice", "jev_select_evidence", "jev_rank_transcripts", "jev_screen_frames"}
SOURCES = {"mcp", "cli", "python"}
OUTCOMES = {"success", "partial_response", "http_error", "network_error", "invalid_response", "other_error"}


def activity_path():
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "jev-activity.sqlite"


def _model_name(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:~-]{1,160}", value) else "unknown"


def _token_count(value):
    return value if type(value) is int and 0 <= value <= 10_000_000 else None


def record_call(*, operation, source, outcome, model, duration_ms, usage=None, http_status=None):
    """Best-effort logging; store only allowed metadata and never request data."""
    usage = usage if isinstance(usage, dict) else {}
    row = (int(time.time()), operation if operation in OPERATIONS else "unknown",
           source if source in SOURCES else "python", outcome if outcome in OUTCOMES else "other_error",
           _model_name(model), max(0, min(int(duration_ms), 3_600_000)),
           _token_count(usage.get("input_tokens")), _token_count(usage.get("output_tokens")),
           http_status if type(http_status) is int and 100 <= http_status <= 599 else None)
    try:
        path = activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=1)) as db:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS calls ("
                           "timestamp INTEGER NOT NULL, operation TEXT NOT NULL, source TEXT NOT NULL, "
                           "outcome TEXT NOT NULL, model TEXT NOT NULL, duration_ms INTEGER NOT NULL, "
                           "input_tokens INTEGER, output_tokens INTEGER, http_status INTEGER)")
                db.execute("INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        return True
    except (OSError, sqlite3.Error):
        return False


def status(hours=24):
    """Read recent local records; success means the API returned structured answers."""
    if type(hours) is not int or not 1 <= hours <= 24 * 30:
        raise ValueError("hours 必须是 1 至 720 的整数。")
    path = activity_path()
    cutoff = int(time.time()) - hours * 3600
    if not path.is_file():
        return {"status": "no_records", "hours": hours, "requests": 0, "successful": 0,
                "failed": 0, "recent": [], "history_before_tracking": "unavailable",
                "note": "没有本地记录；旧 MCP 进程未重载或记录写入失败时也可能缺失。"}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            totals = db.execute("SELECT COUNT(*), SUM(outcome = 'success') FROM calls WHERE timestamp >= ?", (cutoff,)).fetchone()
            first = db.execute("SELECT MIN(timestamp) FROM calls").fetchone()[0]
            rows = db.execute("SELECT timestamp, operation, source, outcome, model, duration_ms, "
                              "input_tokens, output_tokens, http_status FROM calls WHERE timestamp >= ? "
                              "ORDER BY timestamp DESC, rowid DESC LIMIT 10", (cutoff,)).fetchall()
    except (OSError, sqlite3.Error):
        return {"status": "unavailable", "hours": hours, "history_before_tracking": "unavailable"}
    total, successful = totals[0], totals[1] or 0
    recent = [{"time_utc": datetime.fromtimestamp(row[0], timezone.utc).isoformat().replace("+00:00", "Z"),
               "operation": row[1], "source": row[2], "outcome": row[3], "model": row[4],
               "duration_ms": row[5], "input_tokens": row[6], "output_tokens": row[7], "http_status": row[8]}
              for row in rows]
    tracking_started = datetime.fromtimestamp(first, timezone.utc).isoformat().replace("+00:00", "Z") if first else None
    return {"status": "ok", "hours": hours, "tracking_started_utc": tracking_started, "requests": total, "successful": successful,
            "failed": total - successful, "recent": recent, "history_before_tracking": "unavailable",
            "note": "仅统计记录开始后的 API 请求；旧 MCP 进程或写入失败时可能缺失。failed 包括失败和响应不完整；成功不自动证明提升质量，也不区分真实任务和测试。"}
