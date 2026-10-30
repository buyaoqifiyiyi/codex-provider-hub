"""Find a parent transcript by UUID; never print its contents."""
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

UUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)


def main(identity):
    if not UUID.fullmatch(identity):
        return None
    home=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    database=home/'state_5.sqlite'
    with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=0.4) as conn:
        row=conn.execute('select rollout_path from threads where id=?',(identity,)).fetchone()
    return row[0] if row and isinstance(row[0],str) else None


if __name__=='__main__':
    try: result=main(sys.argv[1])
    except Exception: result=None
    print(json.dumps({'path':result},ensure_ascii=True))
