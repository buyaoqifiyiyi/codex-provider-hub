"""Read only local disable metadata; never load credentials or contact a service."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys


def disabled_models(database):
    database = Path(database)
    try:
        database.stat()
    except FileNotFoundError:
        return []  # Hub has not been installed for this user.
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='disabled_connections'").fetchone():
            return []  # Backward-compatible with the pre-disable schema.
        children = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='connection_models'").fetchone())
        official = ("SELECT m.model FROM connection_models m JOIN providers p ON p.id=m.provider_id JOIN disabled_connections d ON d.provider_id=p.id WHERE p.protocol='codex_subscription'"
                    if children else "SELECT p.model FROM providers p JOIN disabled_connections d ON d.provider_id=p.id WHERE p.protocol='codex_subscription'")
        return sorted({row[0] for row in db.execute(
            'SELECT r.alias FROM model_routes r JOIN disabled_connections d ON d.provider_id=r.provider_id UNION '+official)})



if __name__ == '__main__':
    try:
        database = Path(sys.argv[1]) if len(sys.argv) == 2 else Path(os.environ['LOCALAPPDATA']) / 'CodexProviderHub' / 'profiles.sqlite'
        print(json.dumps({'models': disabled_models(database)}, ensure_ascii=True))
    except Exception:
        print('MODEL_DISABLE_STATE_UNAVAILABLE', file=sys.stderr)
        sys.exit(2)
