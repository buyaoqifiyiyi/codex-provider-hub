"""Classify an active model binding from local metadata; never print endpoint or credentials."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from storage import MODEL_ADAPTERS, ProfileStore



def active_bindings(db):
    """One read snapshot; hashes ciphertext for revision, never decrypts it."""
    result = {}
    rows = db.execute('SELECT b.alias,b.provider_id,b.model,b.revision,b.account FROM engine_bindings b '
                      'JOIN model_routes r ON r.alias=b.alias AND r.provider_id=b.provider_id AND r.model=b.model '
                      'JOIN connection_models m ON m.provider_id=b.provider_id AND m.model=b.model '
                      'WHERE NOT EXISTS(SELECT 1 FROM disabled_connections d WHERE d.provider_id=b.provider_id)').fetchall()
    for alias, connection, model, revision, account in rows:
        profile = db.execute('SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?', (connection,)).fetchone()
        if (not profile or profile[5] is None or profile[2] not in MODEL_ADAPTERS
                or not isinstance(account,str) or not account
                or ProfileStore._content_revision(db,profile) != revision):
            continue
        url = urlsplit(profile[3])
        if url.scheme not in ('https','http') or not url.hostname or any((url.username,url.password,url.query,url.fragment)):
            continue
        identity = hashlib.sha256(json.dumps([connection,model,revision,account],separators=(',',':')).encode()).hexdigest()
        result[alias] = {'revision':identity,'model':model,'connection':connection,'display_name':profile[1]}
    return result


def binding_kind(database, alias=None):
    if not isinstance(alias, str) or not alias or alias.strip() != alias:
        return {'kind':'unknown','revision':None}
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)) as db:
        db.execute('BEGIN')
        binding = active_bindings(db).get(alias)
        if not binding:
            disabled = db.execute('SELECT 1 FROM engine_bindings b JOIN disabled_connections d ON d.provider_id=b.provider_id WHERE b.alias=?',(alias,)).fetchone()
            exists = db.execute('SELECT 1 FROM engine_bindings WHERE alias=?',(alias,)).fetchone()
            return {'kind':'disabled' if disabled else 'unknown' if exists else 'unbound','revision':None}
        return dict(binding,kind='bound')


if __name__ == '__main__':
    try:
        print(json.dumps(binding_kind(Path(os.environ['LOCALAPPDATA']) / 'CodexProviderHub' / 'profiles.sqlite', sys.argv[1] if len(sys.argv)>1 else None)))
    except Exception:
        print(json.dumps({'kind':'unknown','revision':None}))
