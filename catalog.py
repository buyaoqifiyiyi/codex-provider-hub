"""Build a local Codex model catalog from a preserved baseline and active routes."""
import http.client
import base64
import json
import os
from pathlib import Path
import tomllib
import uuid

from service import _read_capability
from storage import ProfileStore, default_data_root, effective_model_settings
from native_catalog import native_model_ids

MAX_CATALOG = 16 * 1024 * 1024


def apply_model_settings(row: dict, settings: dict) -> None:
    if settings["context_window"] is not None:
        row["context_window"] = settings["context_window"]
    if settings["compact_limit"] is not None:
        if settings["compact_limit"] > row.get("context_window", 0):
            raise ValueError("自动压缩阈值不能大于模型的上下文窗口。")
        row["auto_compact_token_limit"] = settings["compact_limit"]
    levels = settings["reasoning_levels"]
    if levels is not None:
        descriptions = {item.get("effort"): item.get("description", item.get("effort"))
                        for item in row.get("supported_reasoning_levels", []) if isinstance(item, dict)}
        row["supported_reasoning_levels"] = [{"effort": value, "description": descriptions.get(value, value)} for value in levels]
        if settings["default_effort"] is not None:
            row["default_reasoning_level"] = settings["default_effort"]
        elif row.get("default_reasoning_level") not in levels:
            row["default_reasoning_level"] = levels[0]



def recover_publication(store):
    marker=store.root/"catalog-publication.json"
    if not marker.exists(): return
    data=json.loads(marker.read_text(encoding="utf-8"))
    with store.connection() as db:
        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='catalog_publications'").fetchone()
        committed=exists and db.execute("SELECT 1 FROM catalog_publications WHERE id=?",(data["id"],)).fetchone()
    if not committed:
        target=store.root/"catalog.json"
        if data["previous"] is None: target.unlink(missing_ok=True)
        else:
            temp=target.with_suffix(".recover.tmp")
            temp.write_bytes(base64.b64decode(data["previous"],validate=True));os.replace(temp,target)
    marker.unlink(missing_ok=True)


def transactional_publish(store,operation):
    if getattr(store,"_catalog_publication",False): return operation()
    with store.atomic() as db:
        recover_publication(store)
        target=store.root/"catalog.json";marker=store.root/"catalog-publication.json"
        previous=target.read_bytes() if target.exists() else None
        ident=uuid.uuid4().hex
        db.execute("CREATE TABLE IF NOT EXISTS catalog_publications(id TEXT PRIMARY KEY)")
        _write_atomic(marker,{"id":ident,"previous":base64.b64encode(previous).decode("ascii") if previous is not None else None})
        def restore():
            if previous is None: target.unlink(missing_ok=True)
            else:
                temporary=target.with_suffix(".rollback.tmp");temporary.write_bytes(previous);os.replace(temporary,target)
            marker.unlink(missing_ok=True)
        store._rollback_actions.append(restore)
        store._commit_actions.append(lambda:marker.unlink(missing_ok=True))
        db.execute("INSERT INTO catalog_publications VALUES (?)",(ident,))
        store._catalog_publication=True
        try: return operation()
        finally: store._catalog_publication=False


def compile_catalog(baseline: dict, engine_catalog: dict, store: ProfileStore) -> dict:
    # One snapshot covers the complete alias set; content revisions can be reused
    # while SQLite excludes concurrent writers.
    with store.atomic():
        return _compile_catalog(baseline,engine_catalog,store)


def _compile_catalog(baseline: dict, engine_catalog: dict, store: ProfileStore) -> dict:
    models = baseline.get("models") if isinstance(baseline, dict) else None
    discovered = engine_catalog.get("models") if isinstance(engine_catalog, dict) else None
    if not isinstance(models, list) or not isinstance(discovered, list):
        raise ValueError("模型目录无效。")
    aliases = set(store.engine_aliases())
    native = native_model_ids(root=store.root)
    if aliases & native:
        raise ValueError("模型目录与连接别名存在冲突。")
    native -= store.known_engine_aliases()
    result = dict(baseline, models=[dict(item) for item in models if isinstance(item, dict)])
    for row in result["models"]:
        slug = row.get("slug")
        if isinstance(slug, str) and slug not in native and slug not in aliases:
            row["visibility"] = "hide"
    known = {item.get("slug") for item in result["models"]}
    incoming = {item.get("slug"): item for item in discovered if isinstance(item, dict) and isinstance(item.get("slug"), str)}
    profiles = {item["id"]: item for item in store.list()}
    official_seen = set()
    for profile in profiles.values():
        if profile["protocol"] != "codex_subscription":
            continue
        for model in profile.get("models",[profile["model"]]):
            if model in official_seen:
                continue
            official_seen.add(model)
            if not store.official_model_disabled(model) and model not in known:
                raise ValueError("所选订阅模型不在本机模型目录中，未发布目录。")
            for row in result["models"]:
                if row.get("slug") == model:
                    if store.official_model_disabled(model):
                        row["visibility"] = "hide"
                    else:
                        row["visibility"] = "list"
                        apply_model_settings(row, store.model_settings(profile["id"],model))
    for alias in sorted(aliases):
        binding = store.get_engine_binding(alias)
        if not binding:
            raise ValueError("内置引擎尚未提供已启用模型的目录。")
        if profiles.get(binding["id"], {}).get("disabled"):
            for row in result["models"]:
                if row.get("slug") == alias:
                    row["display_name"] = binding["model"]
                    row["description"] = "供应商：" + binding["name"]
                    row["visibility"] = "hide"
            continue
        name = binding["account"].split("/", 1)[0]
        advertised = incoming.get(alias) or incoming.get(name + "/" + binding["model"].replace("/", "-"))
        if not advertised:
            raise ValueError("内置引擎尚未提供已启用模型的目录。")
        if alias in known:
            row = next(item for item in result["models"] if item.get("slug") == alias)
            row["display_name"] = binding["model"]
            row["description"] = "供应商：" + binding["name"]
            row["visibility"] = "list"
            apply_model_settings(row, effective_model_settings(store.model_settings(binding["id"],binding["model"])))
            continue
        row = dict(advertised)
        row["slug"] = alias
        row["display_name"] = binding["model"]
        row["description"] = "供应商：" + binding["name"]
        row["comp_hash"] = None
        row["visibility"] = "list"
        apply_model_settings(row, effective_model_settings(store.model_settings(binding["id"],binding["model"])))
        result["models"].append(row)
        known.add(alias)
    return result


def _read_json(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) > MAX_CATALOG:
        raise ValueError("模型目录过大。")
    return json.loads(data)


def _write_atomic(path: Path, data: dict):
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CATALOG:
        raise ValueError("模型目录过大。")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(encoded)
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def refresh(root: Path | None = None, *, codex_home: Path | None = None, store=None) -> Path:
    root = Path(root) if root is not None else default_data_root()
    codex_home = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME") or Path.home()/".codex")
    root.mkdir(parents=True, exist_ok=True)
    baseline_path = root / "baseline-catalog.json"
    if not baseline_path.exists():
        config = tomllib.loads((codex_home/"config.toml").read_text(encoding="utf-8-sig"))
        source = config.get("model_catalog_json")
        if not isinstance(source, str):
            raise ValueError("当前 Codex 没有可继承的模型目录。")
        base = _read_json(Path(source))
        if not isinstance(base.get("models"), list):
            raise ValueError("当前 Codex 模型目录无效。")
        _write_atomic(baseline_path, base)
    baseline = _read_json(baseline_path)
    state = _read_json(root / "gateway-state.json")
    token = _read_capability(root)
    conn = http.client.HTTPConnection("127.0.0.2", state["inner_port"], timeout=5)
    try:
        conn.request("GET", "/v1/models?client_version=0.164.0", headers={"x-opencodex-api-key": token})
        response = conn.getresponse()
        data = response.read(MAX_CATALOG + 1)
        if response.status != 200 or len(data) > MAX_CATALOG:
            raise ValueError("无法读取内置引擎的模型目录。")
        engine_catalog = json.loads(data)
    finally:
        conn.close()
    result = compile_catalog(baseline, engine_catalog, store or ProfileStore(root))
    path = root / "catalog.json"
    publisher=store or ProfileStore(root)
    transactional_publish(publisher,lambda:_write_atomic(path,result))
    return path
