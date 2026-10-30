"""One-way, read-only import of existing Codex provider credentials."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tomllib
from urllib.parse import urlsplit

from storage import ProfileStore


def import_existing(store: ProfileStore, database: Path, jev_settings: Path | None = None) -> dict:
    from engine_control import activation_lock
    with activation_lock(store.root):
        return _import_existing_unlocked(store, database, jev_settings)


def _import_existing_unlocked(store: ProfileStore, database: Path, jev_settings: Path | None = None) -> dict:
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        rows = db.execute("SELECT id,name,settings_config,meta,is_current FROM providers WHERE app_type='codex'").fetchall()
    jev = {}
    if jev_settings and jev_settings.is_file():
        jev = json.loads(jev_settings.read_text(encoding="utf-8"))
    with closing(store._connect()) as db:
        db.execute("CREATE TABLE IF NOT EXISTS legacy_imports ("
                   "source_id TEXT NOT NULL, model TEXT NOT NULL, provider_id TEXT NOT NULL, "
                   "PRIMARY KEY(source_id,model))")
    imported, skipped, current_profile, jev_profile = 0, 0, None, None
    for source_id, name, raw, raw_meta, is_current in rows:
        try:
            data = json.loads(raw); metadata = json.loads(raw_meta) if raw_meta else {}
            config = tomllib.loads(data.get("config", ""))
            selected = config.get("model_provider")
            base_url = config.get("model_providers", {}).get(selected, {}).get("base_url")
            key = data.get("auth", {}).get("OPENAI_API_KEY")
            if not isinstance(key, str) or not key.strip() or not isinstance(base_url, str):
                continue
            models = []
            if isinstance(config.get("model"), str) and config["model"]:
                models.append(config["model"])
            for entry in data.get("modelCatalog", {}).get("models", []):
                if isinstance(entry, dict) and isinstance(entry.get("model"), str):
                    models.append(entry["model"])
            for model in dict.fromkeys(models):
                protocol = {"openai_chat": "chat_completions", "openai_responses": "responses"}.get(metadata.get("apiFormat"))
                endpoint = base_url
                if (name == jev.get("provider_name") and model == jev.get("model")
                        and urlsplit(base_url).hostname == urlsplit(str(jev.get("endpoint", ""))).hostname):
                    protocol, endpoint = "system_one", jev["endpoint"]
                if protocol is None:
                    skipped += 1; continue
                with closing(store._connect()) as db:
                    existing = db.execute("SELECT provider_id FROM legacy_imports WHERE source_id=? AND model=?",
                                          (source_id, model)).fetchone()
                if existing:
                    ident = existing[0]
                    if not any(item["id"] == ident for item in store.list()):
                        skipped += 1
                        continue  # A deliberately deleted imported connection stays deleted.
                else:
                    saved = store.save(name, protocol, endpoint, model, key)
                    ident = saved["id"]
                    with closing(store._connect()) as db:
                        with db:
                            db.execute("INSERT INTO legacy_imports(source_id,model,provider_id) VALUES (?,?,?)",
                                       (source_id, model, ident))
                    imported += 1
                if protocol == "system_one": jev_profile = ident
                if is_current and model == config.get("model"): current_profile = ident
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            skipped += 1
    return {"imported": imported, "skipped": skipped, "current_profile": current_profile, "jev_profile": jev_profile}


def import_local(jev_settings: Path | None = None) -> dict:
    home = Path.home()
    source = home / ".cc-switch/cc-switch.db"
    return import_existing(ProfileStore(), source, jev_settings)
