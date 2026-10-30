"""Read current local routing evidence without starting public or model requests."""
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

try:
    from storage import ProfileStore
    from capability_service import public_evidence
    from public_evaluations import cache_matches_source
    store=ProfileStore()
    evidence={}
    bindings={}
    for alias in store.engine_aliases():
        binding=store.get_engine_binding(alias)
        if binding and not store.is_disabled(binding['id']): bindings[alias]=binding
    for alias,binding in bindings.items():
        rows=public_evidence(store,binding['id'],binding['model'])
        latest={}
        for row in rows:
            if row.get('status') not in {'passed','failed','error'} or not row.get('tested_at'): continue
            key=(row.get('purpose'),row.get('channel'),row.get('effort'))
            if key not in latest or row['tested_at'] > latest[key]['tested_at']:
                latest[key]={k:row.get(k) for k in ('purpose','channel','effort','status','tested_at')}
        evidence[alias]=list(latest.values())
    references={}
    database=store.root/'public_evaluations.sqlite3'
    if database.is_file():
        with sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5) as db:
            for alias,binding in bindings.items():
                row=db.execute('SELECT payload,expires FROM public_references WHERE model=?',
                               (binding['model'],)).fetchone()
                if row and row[0] and row[1] > time.time():
                    value=json.loads(row[0])
                    if isinstance(value,dict) and cache_matches_source(store.root,binding['model'],value):
                        rows=value.get('results') if isinstance(value.get('results'),list) else []
                        references[alias]={'status':value.get('status'),'results':[
                            {k:r.get(k) for k in ('benchmark','task_id','unit','conditions_fingerprint','score','verified')}
                            for r in rows[:100] if isinstance(r,dict)]}
    print(json.dumps({'ok':True,'evidence':evidence,'references':references},ensure_ascii=True))
except Exception:
    # Errors may include private configuration; do not print exception text.
    print(json.dumps({'ok':False,'evidence':{},'references':{}}))
