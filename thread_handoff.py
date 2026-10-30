"""Identify the current local conversation without disclosing its history."""
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)
MAX_TRANSCRIPT_BYTES = 128 * 1024 * 1024


def normalized_path(path):
    """Treat Windows extended-length paths and normal paths as the same path."""
    value = str(path)
    if os.name == "nt" and value.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + value[8:])
    if os.name == "nt" and value.startswith("\\\\?\\"):
        return Path(value[4:])
    return Path(value)


def verified_compacted_thread(headers, request, *, codex_home=None):
    """Return an ID only if both headers and the latest local checkpoint agree."""
    try:
        thread, session = headers.get_all("thread-id", []), headers.get_all("session-id", [])
        if len(thread) != 1 or len(session) != 1 or thread[0] != session[0] or not UUID.fullmatch(thread[0]):
            return None
        items = request.get("input")
        if not isinstance(items, list): return None
        candidates = {item.get("encrypted_content") for item in items if isinstance(item, dict)
                      and item.get("type") in {"compaction", "compaction_summary", "context_compaction"}
                      and isinstance(item.get("encrypted_content"), str)
                      and item["encrypted_content"]
                      and not item["encrypted_content"].startswith(("ocx1:", "ocx2:"))}
        if not candidates: return None
        home = Path(codex_home or os.environ.get("CODEX_HOME") or Path.home()/".codex")
        databases = [p for p in home.glob("state_*.sqlite") if re.fullmatch(r"state_\d+\.sqlite", p.name)
                     and p.is_file() and not p.is_symlink()]
        if not databases: return None
        database = max(databases, key=lambda p: int(p.stem.split("_")[1]))
        with closing(sqlite3.connect(database.resolve().as_uri()+"?mode=ro", uri=True, timeout=.2)) as db:
            row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (thread[0],)).fetchone()
        if not row or not isinstance(row[0], str): return None
        transcript = Path(row[0])
        if transcript.is_symlink() or not transcript.is_file():
            return None
        before = transcript.stat()
        if before.st_size > MAX_TRANSCRIPT_BYTES: return None
        resolved = normalized_path(transcript.resolve(strict=True))
        if not any(resolved.is_relative_to(normalized_path((home/name).resolve())) for name in ("sessions", "archived_sessions")):
            return None
        latest_matches = False
        matching_session = False
        with transcript.open("rb") as source:
            for raw in source:
                if not raw.endswith(b"\n"): return None
                try: record = json.loads(raw)
                except (ValueError, UnicodeError): return None
                if not isinstance(record, dict): return None
                payload = record.get("payload")
                if record.get("type") == "session_meta":
                    if not isinstance(payload, dict) or str(payload.get("id", "")).lower() != thread[0].lower():
                        return None
                    matching_session = True
                if record.get("type") != "compacted": continue
                history = payload.get("replacement_history") if isinstance(payload, dict) else None
                checkpoints = {item.get("encrypted_content") for item in history if isinstance(item, dict)
                               and item.get("type") in {"compaction", "compaction_summary", "context_compaction"}
                               and isinstance(item.get("encrypted_content"), str)} if isinstance(history, list) else set()
                latest_matches = candidates.issubset(checkpoints)
        after = transcript.stat()
        unchanged = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        return thread[0] if matching_session and latest_matches and unchanged else None
    except (OSError, sqlite3.Error, TypeError, ValueError, AttributeError):
        return None
