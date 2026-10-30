"""Return only the current thread's active turn identity and transcript path."""
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)


def active_turn(file):
    with file.open('rb') as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 16 * 1024 * 1024))
        text = handle.read().decode('utf-8', errors='replace')
    active = None
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        payload = record.get('payload') or {}
        if record.get('type') != 'event_msg':
            continue
        event = payload.get('type')
        if event == 'task_started':
            active = payload.get('turn_id')
        elif event in ('task_complete', 'turn_aborted'):
            active = None
    return active if isinstance(active, str) and UUID.fullmatch(active) else None


def main(session):
    if not UUID.fullmatch(session):
        return None
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    db = home / 'state_5.sqlite'
    with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.4) as conn:
        row = conn.execute('select rollout_path from threads where id=?', (session,)).fetchone()
    if not row or not isinstance(row[0], str):
        return None
    file = Path(row[0])
    turn = active_turn(file)
    return {'session_id':session,'turn_id':turn,'transcript_path':str(file)} if turn else None


if __name__ == '__main__':
    try:
        value = main(sys.argv[1])
    except Exception:
        value = None
    print(json.dumps(value, ensure_ascii=True))
