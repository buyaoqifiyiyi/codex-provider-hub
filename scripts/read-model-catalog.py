"""Read local model IDs/capabilities without loading credentials or making requests."""
import json
import base64
import os
import sqlite3
from contextlib import closing
import pathlib
import sys
import tomllib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from native_catalog import native_model_ids

def managed_models():
    local = os.environ.get('LOCALAPPDATA')
    if not local: return {}, set(), set()
    database = pathlib.Path(local)/'CodexProviderHub'/'profiles.sqlite'
    if not database.is_file(): return {}, set(), set()
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location('hub_binding_metadata', pathlib.Path(__file__).with_name('binding-kind.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            db.execute('BEGIN')
            official = {r[0] for r in db.execute("SELECT m.model FROM connection_models m JOIN providers p ON p.id=m.provider_id WHERE p.protocol='codex_subscription' AND NOT EXISTS(SELECT 1 FROM disabled_connections d WHERE d.provider_id=p.id) AND NOT EXISTS(SELECT 1 FROM connection_models n JOIN providers q ON q.id=n.provider_id JOIN disabled_connections d ON d.provider_id=q.id WHERE q.protocol='codex_subscription' AND n.model=m.model)")}
            known = {r[0] for r in db.execute('SELECT alias FROM engine_bindings UNION SELECT alias FROM model_routes')}
            return module.active_bindings(db), official, known
    except (OSError,sqlite3.Error,ValueError,TypeError):
        return {}, set(), set()


def read_catalog(home):
    config_file = home / 'config.toml'
    config = tomllib.loads(config_file.read_text(encoding='utf-8-sig')) if config_file.exists() else {}
    catalog = config.get('model_catalog_json')
    profile = config.get('profile')
    if profile:
        selected = config['profiles'][profile]
        catalog = selected.get('model_catalog_json', catalog)
    if catalog is not None and (not isinstance(catalog, str) or not catalog):
        raise ValueError('Invalid catalog setting')
    # An explicitly configured directory is authoritative. Never revive removed
    # entries from an older cache when that directory is missing or broken.
    source = pathlib.Path(catalog).expanduser() if catalog is not None else pathlib.Path('models_cache.json')
    if not source.is_absolute():
        source = home / source
    marker=(pathlib.Path(os.environ['LOCALAPPDATA'])/'CodexProviderHub'/'catalog-publication.json') if os.environ.get('LOCALAPPDATA') else None
    previous=None;pending=False
    if marker and marker.exists() and source.name=='catalog.json':
        try:
            journal=json.loads(marker.read_text(encoding='utf-8'))
            database=marker.parent/'profiles.sqlite'
            with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
                exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_publications'").fetchone()
                committed=exists and db.execute('SELECT 1 FROM catalog_publications WHERE id=?',(journal['id'],)).fetchone()
            if not committed:
                pending=True;previous=journal['previous']
        except (OSError,sqlite3.Error,ValueError,KeyError):
            raise ValueError('Catalog publication recovery required') from None
    if pending:
        if previous is None: raise ValueError('Catalog publication not committed')
        document=json.loads(base64.b64decode(previous,validate=True))
    else:
        document = json.loads(source.read_text(encoding='utf-8-sig'))
    entries = document['models']
    if not isinstance(entries, list):
        raise ValueError('Invalid model catalog')
    models = {}
    details = {}
    hub_models, official, known_aliases = managed_models()
    native = native_model_ids(home)
    conflicts = native & known_aliases
    native -= known_aliases
    hub_models = {alias: binding for alias, binding in hub_models.items() if alias not in conflicts}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get('slug'), str):
            continue
        slug=entry['slug']
        if entry.get('visibility') == 'hide' or slug in conflicts or slug not in hub_models and slug not in official and slug not in native:
            continue
        levels = entry.get('supported_reasoning_levels', [])
        if not isinstance(levels, list):
            levels = []
        efforts = [level['effort'] for level in levels
                   if isinstance(level, dict) and isinstance(level.get('effort'), str)]
        # An enabled model with unknown effort still appears as a candidate,
        # but an empty list cannot pass the spawn guard until configured.
        if efforts or slug in hub_models or slug in official or slug in native:
            models[entry['slug']] = list(dict.fromkeys(efforts))
            context = entry.get('context_window')
            if type(context) is int and context > 0:
                details[slug] = {'context_window':context,'source':'catalog_configuration','verified':False}
    return {'source': str(source.resolve()), 'models': models,
            'hubModels': {alias:binding for alias,binding in hub_models.items() if alias in models},
            'managedOfficialModels': sorted(official.intersection(models)),
            'nativeModels': sorted(native.intersection(models)), 'modelDetails':details}


if __name__ == '__main__':
    try:
        print(json.dumps(read_catalog(pathlib.Path(sys.argv[1]).resolve()), ensure_ascii=True))
    except Exception:
        # Configuration values and exception tracebacks can contain private data.
        print('MODEL_CATALOG_UNAVAILABLE', file=sys.stderr)
        sys.exit(2)
