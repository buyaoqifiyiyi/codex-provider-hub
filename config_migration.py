"""Reversible, narrow Codex configuration migration; credentials stay local."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import stat
import tomllib
import uuid
from urllib.parse import urlsplit

import tomlkit

from service import _read_capability
from storage import ProfileStore, _dpapi, default_data_root
import ocx_service
from front_proxy import named_route_ready
from native_catalog import native_model_ids
from guard_verification import guard_verified
from guard_verification import OLD_PLUGIN
from autostart import startup_verified


def plan_config(source: str, root: Path, port: int, token: str, enable_jev: bool, codex_home: Path | None = None) -> str:
    document = tomlkit.parse(source)
    before = tomllib.loads(source)
    if before.get("profile"):
        raise ValueError("当前使用 Codex 配置档案，不能自动迁移。")
    if before.get("model_provider") != "custom":
        raise ValueError("当前 Codex 线路不是已识别的本机 custom 线路。")
    route_before = before.get("model_providers", {}).get("custom", {})
    if any(key in route_before for key in ("env_key", "experimental_bearer_token", "auth")):
        raise ValueError("现有线路包含单独的认证覆盖，不能自动迁移。")
    for field in ("http_headers", "env_http_headers"):
        value = route_before.get(field, {})
        if not isinstance(value, dict) or any(str(key).lower() in {"authorization", "chatgpt-account-id"} for key in value):
            raise ValueError("现有线路包含单独的认证请求头，不能自动迁移。")
        if field == "env_http_headers" and any(str(key).lower() == "x-provider-hub-token" for key in value):
            raise ValueError("现有线路包含冲突的连接中心环境变量请求头。")
    configured_url = route_before.get("base_url")
    if configured_url is None:
        # The user may have switched CCS back to official direct to recover an
        # active chat. Accept only that known default; do not guess overrides.
        official_default = (route_before.get("name") == "OpenAI"
                            and route_before.get("requires_openai_auth") is True
                            and route_before.get("wire_api") == "responses"
                            and not any(before.get(key) for key in ("openai_base_url", "chatgpt_base_url"))
                            and not any(os.environ.get(key) for key in ("OPENAI_BASE_URL", "OPENAI_API_BASE")))
        if not official_default:
            raise ValueError("当前 Codex 线路不是已识别的官方直连或本机代理。")
    else:
        try:
            url = urlsplit(configured_url)
            if (url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port
                    or url.path.rstrip("/") != "/v1" or url.username or url.password or url.query or url.fragment):
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("当前 Codex 线路不是已识别的官方直连或本机代理。") from None
    if not (root / "catalog.json").is_file():
        raise ValueError("模型目录尚未准备好。")
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    slugs = {item.get("slug") for item in catalog.get("models", []) if isinstance(item, dict) and item.get("visibility") != "hide"}
    current_model = before.get("model")
    target_model = current_model
    store = ProfileStore(root)
    if isinstance(current_model, str) and current_model not in slugs:
        candidates = [alias for alias in store.engine_aliases() if alias in slugs
                      and (binding := store.get_engine_binding(alias)) and binding["model"] == current_model]
        target_model = candidates[0] if len(candidates) == 1 else None
    if not isinstance(target_model, str) or target_model not in slugs:
        raise ValueError("当前模型尚未映射到可用线路。")
    binding = store.get_engine_binding(target_model)
    aliases = store.known_engine_aliases()
    native = native_model_ids(home=codex_home, root=root)
    if target_model in aliases and target_model in native:
        raise ValueError("模型目录与连接别名存在冲突。")
    native -= aliases
    managed = any(p["protocol"] == "codex_subscription" and not p["disabled"]
                  and target_model in p.get("models", []) for p in store.list())
    if not binding and (target_model in aliases or target_model not in native and not managed):
        raise ValueError("当前模型没有可确认的宿主身份或线路绑定。")
    route = document["model_providers"]["custom"]
    headers_before = route_before.get("http_headers", {})
    if not isinstance(headers_before, dict):
        raise ValueError("原有自定义请求头格式无效。")
    prior_capability = [value for key, value in headers_before.items() if key.lower() == "x-provider-hub-token"]
    if prior_capability and prior_capability != [token]:
        raise ValueError("当前线路包含其他连接中心的标识。")
    if "http_headers" not in route:
        route["http_headers"] = tomlkit.inline_table()
    headers = route["http_headers"]
    capability_name = next((key for key in headers_before if key.lower() == "x-provider-hub-token"), "X-Provider-Hub-Token")
    headers[capability_name] = token
    route["base_url"] = f"http://127.0.0.1:{port}/v1"
    route["wire_api"] = "responses"
    route["requires_openai_auth"] = True
    route["supports_websockets"] = False
    document["model_catalog_json"] = str(root / "catalog.json")
    if isinstance(before.get("plugins", {}).get(OLD_PLUGIN), dict):
        document["plugins"][OLD_PLUGIN]["enabled"] = False
    if target_model != current_model:
        document["model"] = target_model
    legacy_name = None
    servers = before.get("mcp_servers", {})
    if enable_jev and isinstance(servers, dict):
        shared = root / "shared" / "jev_hub.py"
        known = [root / "shared" / "jev_tools" / "server.py", Path(__file__).resolve().parent / "jev_tools" / "server.py"]
        for name, entry in servers.items():
            args = entry.get("args") if isinstance(entry, dict) else None
            if not isinstance(args, list) or len(args) != 1 or not isinstance(args[0], str):
                continue
            candidate = Path(args[0]).expanduser()
            is_shared = str(candidate).replace("/", "\\").casefold() == str(shared).replace("/", "\\").casefold()
            is_original = (candidate.name == "server.py" and candidate.is_file() and not candidate.is_symlink()
                           and any(path.is_file() and candidate.read_bytes() == path.read_bytes() for path in known))
            if is_shared or is_original:
                if legacy_name is not None:
                    raise ValueError("发现多个旧 Jev 入口，无法自动迁移。")
                legacy_name = name
        if legacy_name is not None:
            if not shared.is_file():
                raise ValueError("Jev 新入口尚未准备好。")
            document["mcp_servers"][legacy_name]["command"] = str(root / "runtime/Scripts/python.exe")
            document["mcp_servers"][legacy_name]["args"] = [str(shared)]
    result = tomlkit.dumps(document)
    after = tomllib.loads(result)
    expected = deepcopy(before)
    target = expected["model_providers"]["custom"]
    target.update({"base_url": f"http://127.0.0.1:{port}/v1", "wire_api": "responses",
                   "requires_openai_auth": True, "supports_websockets": False})
    target.setdefault("http_headers", {})[capability_name] = token
    expected["model_catalog_json"] = str(root / "catalog.json")
    expected["model"] = target_model
    if isinstance(expected.get("plugins", {}).get(OLD_PLUGIN), dict):
        expected["plugins"][OLD_PLUGIN]["enabled"] = False
    if legacy_name is not None:
        expected["mcp_servers"][legacy_name]["command"] = str(root / "runtime/Scripts/python.exe")
        expected["mcp_servers"][legacy_name]["args"] = [str(root / "shared/jev_hub.py")]
    if after != expected:
        raise ValueError("配置验证发现了未预期的变化。")
    return result


def _secure_replace(target: Path, data: bytes, original_security):
    import win32security
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        win32security.SetFileSecurity(str(temporary), win32security.DACL_SECURITY_INFORMATION, original_security)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if fd >= 0: os.close(fd)
        if temporary.exists(): temporary.unlink()


def plan_per_model_context(source: str, catalog: dict, catalog_path: Path) -> str:
    """Remove a global override only when current model windows are preserved."""
    before = tomllib.loads(source)
    window = before.get("model_context_window")
    if window is None:
        return source
    if (type(window) is not int or window <= 0 or before.get("profile")
            or before.get("model_provider") != "custom"
            or Path(before.get("model_catalog_json", "")).resolve() != catalog_path.resolve()):
        raise ValueError("当前配置不是可安全迁移的连接中心模型目录。")
    profiles = before.get("profiles", {})
    if not isinstance(profiles, dict) or any(isinstance(value, dict) and "model_context_window" in value for value in profiles.values()):
        raise ValueError("其他配置档案包含上下文覆盖，未自动迁移。")
    models = catalog.get("models")
    if (not isinstance(models, list) or not models
            or any(not isinstance(row, dict) or row.get("context_window") != window for row in models)):
        raise ValueError("模型目录中的现有上下文与全局值不一致，未自动迁移。")
    document = tomlkit.parse(source)
    del document["model_context_window"]
    result = tomlkit.dumps(document)
    expected = dict(before)
    del expected["model_context_window"]
    if tomllib.loads(result) != expected:
        raise ValueError("逐模型上下文迁移产生意外配置变化。")
    return result


def apply_per_model_context(root: Path, codex_home: Path) -> dict:
    import msvcrt
    import win32security
    target = codex_home / "config.toml"
    if not target.is_file() or target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
        raise ValueError("Codex 配置文件不可安全迁移。")
    with open(root / "config-migration.lock", "a+b") as lock:
        if os.fstat(lock.fileno()).st_size == 0: lock.write(b"0"); lock.flush()
        lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        old = target.read_bytes()
        catalog_path = root / "catalog.json"
        planned = plan_per_model_context(old.decode("utf-8-sig"),
                                         json.loads(catalog_path.read_text(encoding="utf-8")), catalog_path)
        new = (b"\xef\xbb\xbf" if old.startswith(b"\xef\xbb\xbf") else b"") + planned.encode("utf-8")
        if old == new:
            return {"changed": False}
        backups = root / "backups"; backups.mkdir(parents=True, exist_ok=True)
        backup = backups / ("codex-context-" + uuid.uuid4().hex + ".dpapi")
        with open(backup, "xb") as out:
            out.write(_dpapi(old)); out.flush(); os.fsync(out.fileno())
        if target.read_bytes() != old:
            raise ValueError("Codex 配置在迁移期间发生变化，未覆盖。")
        security = win32security.GetFileSecurity(str(target), win32security.DACL_SECURITY_INFORMATION)
        _secure_replace(target, new, security)
        if target.read_bytes() != new:
            raise ValueError("逐模型上下文配置写入后未通过核对。")
        return {"changed": True, "backup": str(backup)}


def apply_config(root: Path | None = None, codex_home: Path | None = None, *, check_service=True) -> dict:
    import msvcrt
    import win32security
    root = Path(root) if root is not None else default_data_root()
    codex_home = Path(codex_home) if codex_home is not None else Path(os.environ.get("CODEX_HOME") or Path.home()/".codex")
    target = codex_home / "config.toml"
    if not target.is_file() or target.is_symlink() or not stat.S_ISREG(target.stat().st_mode):
        raise ValueError("Codex 配置文件不可安全迁移。")
    state = ocx_service.status(root)
    if check_service and not state.get("running"):
        raise ValueError("连接中心服务尚未就绪。")
    if check_service and not guard_verified(root, codex_home, allow_old=True):
        raise ValueError("合并后的子代理守卫尚未在同版 Codex 宿主验证并信任。")
    if check_service and not startup_verified(root):
        raise ValueError("内置服务的登录自启动尚未验证。")
    if not state.get("port"):
        raise ValueError("内置服务尚未启动。")
    token = _read_capability(root)
    enable_jev = bool(ProfileStore(root).get_jev_profile_id())
    lock = open(root / "config-migration.lock", "a+b")
    try:
        if os.fstat(lock.fileno()).st_size == 0: lock.write(b"0"); lock.flush()
        lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        old = target.read_bytes()
        source = old.decode("utf-8-sig")
        planned = plan_config(source, root, state["port"], token, enable_jev, codex_home)
        if check_service:
            selected = tomllib.loads(planned).get("model")
            store = ProfileStore(root)
            binding = store.get_engine_binding(selected) if isinstance(selected, str) else None
            if binding:
                if not named_route_ready(store, root / "engine-state", selected):
                    raise ValueError("当前选择的模型线路未就绪。")
            elif not state.get("official_route_ready"):
                raise ValueError("当前选择的模型线路未就绪。")
        new = (b"\xef\xbb\xbf" if old.startswith(b"\xef\xbb\xbf") else b"") + planned.encode("utf-8")
        if old == new:
            return {"changed": False, "jev_configured": enable_jev}
        backups = root / "backups"; backups.mkdir(parents=True, exist_ok=True)
        backup = backups / ("codex-config-" + uuid.uuid4().hex + ".dpapi")
        with open(backup, "xb") as out:
            out.write(_dpapi(old)); out.flush(); os.fsync(out.fileno())
        if target.read_bytes() != old:
            raise ValueError("Codex 配置在迁移期间发生变化，未覆盖。")
        security = win32security.GetFileSecurity(str(target), win32security.DACL_SECURITY_INFORMATION)
        _secure_replace(target, new, security)
        if target.read_bytes() != new:
            raise ValueError("Codex 配置写入后未通过核对。")
        if check_service and not guard_verified(root, codex_home):
            if target.read_bytes() == new:
                _secure_replace(target, old, security)
            raise ValueError("切换后守卫未通过核对，已尝试恢复原配置。")
        return {"changed": True, "jev_configured": enable_jev,
                "backup": str(backup), "original_sha256": hashlib.sha256(old).hexdigest()}
    finally:
        lock.close()
