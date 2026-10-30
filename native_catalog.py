"""Derive host-native model identities from local Codex catalog data."""
import json
import os
from pathlib import Path


def _models(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value.get("models", []) if isinstance(value, dict) and isinstance(value.get("models"), list) else []
    except (OSError, ValueError, TypeError):
        return []


def _saved_native_models(root):
    """Load an optional local record of host identities retained during upgrade."""
    try:
        document = json.loads((root / "native-models.json").read_text(encoding="utf-8-sig"))
        models = document.get("models")
        if (set(document) != {"schemaVersion", "models"} or document["schemaVersion"] != 1
                or not isinstance(models, list) or len(models) > 10000
                or any(not isinstance(name, str) or not name or len(name) > 200
                       or name.strip() != name for name in models)):
            return set()
        return set(models)
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return set()


def native_model_ids(home=None, root=None, excluded_aliases=()):
    """Read host cache identities and explicitly retained local host identities."""
    home = Path(home) if home is not None else Path(os.environ.get("CODEX_HOME") or Path.home()/".codex")
    root = Path(root) if root is not None else Path(os.environ.get("LOCALAPPDATA", ""))/"CodexProviderHub"
    cache = _models(home / "models_cache.json")
    names = {row["slug"] for row in cache if isinstance(row, dict) and isinstance(row.get("slug"), str)
             and row.get("slug") and row.get("visibility") != "hide"}
    return (names | _saved_native_models(root)) - set(excluded_aliases)
