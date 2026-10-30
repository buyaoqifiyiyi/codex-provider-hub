"""Resolve a direct child model from state without reading message content."""
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

UUID=re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$',re.I)
TASK=re.compile(r'^[A-Za-z0-9_-]{1,128}$')


def main(parent,agent_path,target):
    if not UUID.fullmatch(parent) or not isinstance(agent_path,str) or not agent_path.startswith('/'):
        return None
    if UUID.fullmatch(target):
        identity=target;target_path=''
    elif target.startswith(agent_path.rstrip('/')+'/') and '..' not in target:
        identity='';target_path=target
    elif TASK.fullmatch(target):
        identity='';target_path=agent_path.rstrip('/')+'/'+target
    else:
        return None
    home=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    with sqlite3.connect((home/'state_5.sqlite').resolve().as_uri()+'?mode=ro',uri=True,timeout=0.4) as conn:
        row=conn.execute('select t.model from thread_spawn_edges e join threads t on t.id=e.child_thread_id '
                         'where e.parent_thread_id=? and (t.id=? or t.agent_path=?) order by t.created_at desc limit 1',
                         (parent,identity,target_path)).fetchone()
    return row[0] if row and isinstance(row[0],str) else None


if __name__=='__main__':
    try: model=main(sys.argv[1],sys.argv[2],sys.argv[3])
    except Exception: model=None
    print(json.dumps({'model':model},ensure_ascii=True))
