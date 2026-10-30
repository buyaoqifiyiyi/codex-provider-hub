"""Check successful same-turn traffic against the active Provider Hub.

Only a sanitized route classification is printed. Raw logs and config, which
may contain headers, prompts, URLs or credentials, never leave this process.
"""
import json
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import tomllib
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider_hub_route import active_source

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)
# Current Codex batches SQLite log writes at a ten-second interval. Wait for
# that bounded flush rather than accepting prior-turn or configured-only data.
SAME_TURN_LOG_WAIT_SECONDS = 10.5


def classify(observed, expected):
    try:
        actual = urlsplit(observed)
        target = urlsplit(expected)
        if any((actual.username, actual.password, actual.query, actual.fragment,
                target.username, target.password, target.query, target.fragment)):
            return 'unknown'
        if target.scheme != 'http' or target.hostname != '127.0.0.1' or target.path.rstrip('/') != '/v1':
            return 'unknown'
        return ('compatible' if actual.scheme == target.scheme and actual.hostname == target.hostname
                and actual.port == target.port and actual.path == '/v1/responses' else 'incompatible')
    except (TypeError, ValueError):
        return 'unknown'


def main(session_id, turn_id, alias=None, revision=None):
    if not UUID.fullmatch(session_id) or not UUID.fullmatch(turn_id):
        return 'unknown'
    codex_home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    config = tomllib.loads((codex_home / 'config.toml').read_text(encoding='utf-8-sig'))
    profile = config.get('profile')
    if profile:
        return 'unknown'  # Avoid guessing profile-specific routing.
    if config.get('model_provider') != 'custom':
        return 'unknown'
    expected = config.get('model_providers', {}).get('custom', {}).get('base_url')
    if not isinstance(expected, str):
        return 'unknown'
    source = (lambda: active_source(alias, revision)) if alias else active_source
    selected = source()
    if selected is None or selected['base_url'] != expected:
        return 'unknown'
    logs = codex_home / 'logs_2.sqlite'
    # The desktop's SQLite logger can persist the parent HTTP completion after
    # the tool hook has begun. Wait briefly for that same-turn evidence; an
    # absent, stale or failed request still remains unknown.
    end = time.monotonic() + SAME_TURN_LOG_WAIT_SECONDS
    with closing(sqlite3.connect(logs.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.4)) as conn:
        conn.set_progress_handler(lambda: int(time.monotonic() > end), 1000)
        while True:
            rows = conn.execute("select feedback_log_body from logs where thread_id=? and target='codex_http_client::client' "
                                "and feedback_log_body like ? and feedback_log_body like '%Request completed method=POST url=%' "
                                "order by id desc limit 20", (session_id, '%' + turn_id + '%')).fetchall()
            for (body,) in rows:
                if not isinstance(body, str):
                    continue
                turn = re.search(r'turn\.id="?([0-9a-f-]+)', body, re.I)
                request = re.search(r'Request completed method=POST url=(https?://[^\s"}]+) status=(\d{3})', body)
                if turn and turn.group(1).lower() == turn_id.lower() and request:
                    if not (200 <= int(request.group(2)) < 300):
                        return 'unknown'
                    classification = classify(request.group(1), expected)
                    # Health alone never counts as a successful inference.
                    if classification == 'compatible' and source() != selected:
                        return 'unknown'
                    return classification
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
    return 'unknown'


if __name__ == '__main__':
    try:
        route = main(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv)>3 else None, sys.argv[4] if len(sys.argv)>4 else None)
    except Exception:
        route = 'unknown'
    print(json.dumps({'route':route}))
