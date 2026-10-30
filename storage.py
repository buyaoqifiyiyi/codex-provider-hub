"""Local provider profiles and per-user Windows DPAPI-encrypted credentials."""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlsplit

MODEL_ADAPTERS = {"responses": "openai-responses", "chat_completions": "openai-chat",
                  "anthropic_messages": "anthropic", "gemini_native": "google"}
PROTOCOLS = set(MODEL_ADAPTERS) | {"system_one", "codex_subscription"}
OFFICIAL_CODEX_URL = "https://chatgpt.com/backend-api/codex"
EFFORTS = {"none", "low", "medium", "high", "xhigh", "max", "ultra"}
CAPABILITY_PURPOSES = {"connection", "tool", "image", "video", "speech", "audio"}
CAPABILITY_CHANNELS = {"connection", "subagent_gateway", "gemini_media"}
CAPABILITY_STATUSES = {"passed", "failed", "error"}
CAPABILITY_ERROR_CODES = {"timeout", "authentication", "rate_limit", "server", "transport", "format", "unknown"}

ENGINE_ACCOUNT_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


def valid_engine_account(account):
    return isinstance(account,str) and re.fullmatch(ENGINE_ACCOUNT_PATTERN,account) is not None


def validate_model_settings(model, context_window=None, compact_limit=None,
                            reasoning_levels=None, default_effort=None):
    for value, label in ((context_window, "上下文窗口"), (compact_limit, "自动压缩阈值")):
        if value is not None and (type(value) is not int or not 1 <= value <= 100_000_000):
            raise ValueError(f"{label}须为 1 至 100000000 的整数。")
    if compact_limit is not None and context_window is None:
        raise ValueError("设置自动压缩阈值时，请同时填写上下文窗口。")
    if compact_limit is not None and compact_limit > context_window:
        raise ValueError("自动压缩阈值不能大于上下文窗口。")
    if reasoning_levels is not None:
        if (not isinstance(reasoning_levels, list) or not reasoning_levels
                or any(not isinstance(x, str) or x not in EFFORTS for x in reasoning_levels)
                or len(set(reasoning_levels)) != len(reasoning_levels)):
            raise ValueError("思考强度档位无效。")
    if default_effort == "":
        default_effort = None
    if default_effort is not None and (not reasoning_levels or default_effort not in reasoning_levels):
        raise ValueError("默认思考强度须包含在所选档位中。")
    return {"context_window": context_window, "compact_limit": compact_limit,
            "reasoning_levels": reasoning_levels, "default_effort": default_effort}


def effective_model_settings(settings):
    """Keep unspecified model capabilities unknown; preserve explicit settings."""
    return {name: settings.get(name) for name in
            ("context_window", "compact_limit", "reasoning_levels", "default_effort")}


def merge_discovered_model_settings(model, settings, detected):
    """Use provider metadata only where the user left a setting unspecified."""
    result = dict(settings)
    detected = detected if isinstance(detected, dict) else {}
    references = detected.get("reference_sources") if isinstance(detected.get("reference_sources"), dict) else {}
    window = detected.get("context_window")
    if result.get("context_window") is None and "context_window" not in references and type(window) is int and 1 <= window <= 100_000_000:
        result["context_window"] = window
    levels = detected.get("reasoning_levels")
    if (result.get("reasoning_levels") is None and "reasoning_levels" not in references
            and isinstance(levels, list) and levels
            and all(isinstance(level, str) and level in EFFORTS for level in levels)
            and len(set(levels)) == len(levels)):
        result["reasoning_levels"] = list(levels)
    default = detected.get("default_effort")
    if (result.get("default_effort") is None and "reasoning_levels" not in references
            and isinstance(default, str) and default in (result.get("reasoning_levels") or [])):
        result["default_effort"] = default
    return validate_model_settings(model, **result)


def validate_models(model, models, protocol):
    if protocol not in PROTOCOLS:
        raise ValueError("不支持该接口类型。")
    selected = [model] if models is None else models
    if (not isinstance(selected,list) or not selected or len(selected)>5000
            or any(not isinstance(m,str) or not re.fullmatch(r"[A-Za-z0-9_.:/~-]{1,160}",m) for m in selected)
            or len(set(selected)) != len(selected) or selected[0] != model):
        raise ValueError("模型集合须为不重复的有效模型 ID，model 须为首项，最多 5000 项。")
    return list(selected)


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise ValueError("Windows 用户密钥加密不可用。")
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    incoming = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise ValueError("Windows 密钥加密或解密失败。")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def validate_base_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(c.isspace() for c in value):
        raise ValueError("接口地址无效。")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError("接口地址无效。") from None
    if (not url.hostname or url.username or url.password or url.query or url.fragment or port == 0
            or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"}))
            or any(part in {".", ".."} for part in url.path.split("/"))):
        raise ValueError("接口地址须为 HTTPS，或本机 HTTP，且不能包含凭据、查询和片段。")
    return value.rstrip("/")


def validate_provider_url(value: str, protocol: str) -> str:
    endpoint = validate_base_url(value)
    # The pinned google adapter appends /v1beta itself, including for relays.
    if protocol == "gemini_native":
        endpoint = re.sub(r"/v1(?:beta)?$", "", endpoint)
    return endpoint


def default_data_root() -> Path:
    value = os.environ.get("LOCALAPPDATA")
    if not value:
        raise ValueError("找不到当前 Windows 用户的数据目录。")
    return Path(value) / "CodexProviderHub"


class ProfileStore:
    def __init__(self, root: Path | str | None = None, *, allow_offline_migration: bool = False):
        self.allow_offline_migration = allow_offline_migration
        self.root = Path(root) if root is not None else default_data_root()
        self.path = self.root / "profiles.sqlite"

    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        # An explicit offline cutover is required for populated legacy stores:
        # old front processes cannot understand the new binding revision.
        legacy=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='providers'").fetchone()
        if (legacy and db.execute("PRAGMA user_version").fetchone()[0] < 2
                and db.execute("SELECT 1 FROM providers LIMIT 1").fetchone()
                and not self.allow_offline_migration):
            db.close()
            raise ValueError("旧连接库需要离线迁移：先停止旧前置服务及全部旧管理器，再显式允许迁移；不能混跑。")
        db.create_function("provider_hub_writer_version",0,lambda:2)
        db.execute("CREATE TABLE IF NOT EXISTS providers ("
                   "id TEXT PRIMARY KEY, name TEXT NOT NULL, protocol TEXT NOT NULL, "
                   "base_url TEXT NOT NULL, model TEXT NOT NULL, encrypted_key BLOB, "
                   "updated_at INTEGER NOT NULL DEFAULT (unixepoch()))")
        db.execute("CREATE TABLE IF NOT EXISTS model_routes ("
                   "alias TEXT PRIMARY KEY, provider_id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS verified_efforts ("
                   "provider_id TEXT NOT NULL, revision TEXT NOT NULL, effort TEXT NOT NULL, "
                   "PRIMARY KEY(provider_id,revision,effort))")
        db.execute("CREATE TABLE IF NOT EXISTS engine_bindings ("
                   "alias TEXT PRIMARY KEY, provider_id TEXT NOT NULL, revision TEXT NOT NULL, account TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS selections (name TEXT PRIMARY KEY, provider_id TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS model_settings (provider_id TEXT PRIMARY KEY, "
                   "context_window INTEGER, compact_limit INTEGER, reasoning_levels TEXT, default_effort TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS pending_credential_cleanup (account TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE IF NOT EXISTS disabled_connections (provider_id TEXT PRIMARY KEY)")
        # Versioned, transactional migration; opening a legacy store never drops routes.
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            db.execute("BEGIN IMMEDIATE")
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS connection_models (provider_id TEXT NOT NULL, model TEXT NOT NULL, position INTEGER NOT NULL, context_window INTEGER, compact_limit INTEGER, reasoning_levels TEXT, default_effort TEXT, PRIMARY KEY(provider_id,model))")
                for table in ("model_routes", "engine_bindings"):
                    if "model" not in {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}:
                        db.execute("ALTER TABLE " + table + " ADD COLUMN model TEXT")
                    db.execute("UPDATE " + table + " SET model=(SELECT model FROM providers WHERE id=provider_id) WHERE model IS NULL")
                db.execute("INSERT OR IGNORE INTO connection_models SELECT p.id,p.model,0,s.context_window,s.compact_limit,s.reasoning_levels,s.default_effort FROM providers p LEFT JOIN model_settings s ON s.provider_id=p.id")
                for row in db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers").fetchall():
                    revision = self._content_revision(db, row)
                    old_revision = self._revision(row)
                    db.execute("UPDATE engine_bindings SET revision=? WHERE provider_id=? AND revision=?", (revision,row[0],old_revision))
                    db.execute("UPDATE verified_efforts SET revision=? WHERE provider_id=? AND revision=?", (revision,row[0],old_revision))
                db.execute("CREATE TABLE IF NOT EXISTS model_verified_efforts(provider_id TEXT, model TEXT, revision TEXT, effort TEXT, PRIMARY KEY(provider_id,model,revision,effort))")
                db.execute("INSERT OR IGNORE INTO model_verified_efforts SELECT v.provider_id,p.model,v.revision,v.effort FROM verified_efforts v JOIN providers p ON p.id=v.provider_id")
                # Old MCP processes have no writer-version function. Every legacy
                # DML operation aborts rather than updating only the first model.
                for table in ("providers","model_routes","engine_bindings","model_settings","disabled_connections",
                              "verified_efforts","selections","pending_credential_cleanup","connection_models","model_verified_efforts"):
                    for event in ("INSERT","UPDATE","DELETE"):
                        db.execute("CREATE TRIGGER IF NOT EXISTS hub_v2_"+table+"_"+event.lower()+
                                   " BEFORE "+event+" ON "+table+" BEGIN SELECT CASE WHEN provider_hub_writer_version()!=2 THEN RAISE(ABORT,'provider_hub_writer_upgrade_required') END; END")
                db.execute("PRAGMA user_version=2")
        db.execute("CREATE TABLE IF NOT EXISTS model_verified_efforts(provider_id TEXT, model TEXT, revision TEXT, effort TEXT, PRIMARY KEY(provider_id,model,revision,effort))")
        db.execute("CREATE TABLE IF NOT EXISTS retired_engine_providers(name TEXT PRIMARY KEY, account TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS model_probe_receipts(provider_id TEXT NOT NULL, model TEXT NOT NULL, revision TEXT NOT NULL, alias TEXT, effort TEXT NOT NULL, job_id TEXT NOT NULL, tested_at INTEGER NOT NULL DEFAULT (unixepoch()), PRIMARY KEY(provider_id,model,revision,job_id))")
        # Additive schema: retain the v2 writer/route compatibility contract.
        db.execute("CREATE TABLE IF NOT EXISTS capability_evidence ("
                   "provider_id TEXT NOT NULL, model TEXT NOT NULL, identity TEXT NOT NULL, "
                   "purpose TEXT NOT NULL, channel TEXT NOT NULL, effort TEXT NOT NULL, status TEXT NOT NULL, "
                   "tested_at INTEGER NOT NULL, source TEXT NOT NULL, error_code TEXT, "
                   "PRIMARY KEY(provider_id,model,identity,purpose,channel,effort))")
        return db

    @contextmanager
    def connection(self):
        if getattr(self, "_transaction", None) is not None:
            yield self._transaction
        else:
            with closing(self._connect()) as db:
                yield db

    @contextmanager
    def atomic(self):
        if getattr(self, "_transaction", None) is not None:
            yield self._transaction
            return
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            self._transaction = db
            self._rollback_actions = []
            self._commit_actions = []
            self._content_cache = {}
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                # External effects registered by an enclosing save+activation.
                for action in reversed(self._rollback_actions):
                    action()
                raise
            else:
                for action in self._commit_actions:
                    try: action()
                    except OSError: pass  # Durable publication marker permits later recovery.
            finally:
                self._transaction = None
                self._rollback_actions = []
                self._content_cache = {}

    @staticmethod
    def _edit_fingerprint(db, provider_id):
        row = db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not row:
            return None
        children = db.execute("SELECT model,position,context_window,compact_limit,reasoning_levels,default_effort FROM connection_models WHERE provider_id=? ORDER BY position,model", (provider_id,)).fetchall()
        disabled = db.execute("SELECT 1 FROM disabled_connections WHERE provider_id=?", (provider_id,)).fetchone()
        routes = db.execute("SELECT alias,model FROM model_routes WHERE provider_id=? ORDER BY alias", (provider_id,)).fetchall()
        bindings = db.execute("SELECT alias,revision,account,model FROM engine_bindings WHERE provider_id=? ORDER BY alias", (provider_id,)).fetchall()
        selections = db.execute("SELECT name FROM selections WHERE provider_id=? ORDER BY name", (provider_id,)).fetchall()
        raw = json.dumps([row[:5], children, disabled, routes, bindings, selections], ensure_ascii=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw + b"\0" + (row[5] or b"")).hexdigest()

    def edit_fingerprint(self, provider_id):
        with self.connection() as db:
            return self._edit_fingerprint(db, provider_id)

    @staticmethod
    def _revision(row):
        # Kept for legacy binding/evidence migration.
        data = b"\0".join(str(part).encode("utf-8") for part in (row[0], row[2], row[3], row[4]))
        return hashlib.sha256(data + b"\0" + (row[5] or b"")).hexdigest()

    @staticmethod
    def _content_revision(db, row):
        children = db.execute("SELECT model,position,context_window,compact_limit,reasoning_levels,default_effort FROM connection_models WHERE provider_id=? ORDER BY position,model", (row[0],)).fetchall()
        return hashlib.sha256(ProfileStore._revision(row).encode() + json.dumps(children, separators=(",", ":")).encode()).hexdigest()

    def save(self, name, protocol, base_url, model, key=None, provider_id=None, *, settings=None,
             expected_existing=None, models=None, per_model_settings=None, new_id=None):
        selected = validate_models(model, models, protocol)
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("供应商名称须为 1 至 80 个字符。")
        endpoint = OFFICIAL_CODEX_URL if protocol == "codex_subscription" else validate_provider_url(base_url, protocol)
        if protocol == "codex_subscription" and key:
            raise ValueError("Codex 订阅登录使用现有账户，不需要 API 密钥。")
        if key is not None and (not isinstance(key, str) or len(key) > 8192 or any(c in key for c in "\r\n\x00")):
            raise ValueError("密钥格式无效。")
        ciphertext = _dpapi(key.strip().encode()) if key and key.strip() else None
        try:
            ident = str(uuid.UUID(provider_id)) if provider_id is not None else str(uuid.UUID(new_id)) if new_id else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            raise ValueError("供应商标识无效。") from None
        validated = {m: validate_model_settings(m, **v) for m, v in (per_model_settings or {}).items()}
        if set(validated) - set(selected):
            raise ValueError("模型设置不属于所选集合。")
        if settings is not None:
            validated.setdefault(model, validate_model_settings(model, **settings))
        with self.atomic() as db:
            if expected_existing and self._edit_fingerprint(db, expected_existing[0]) != expected_existing[1]:
                raise ValueError("连接在测试期间发生变化，请重新操作。")
            old = db.execute("SELECT protocol,encrypted_key,base_url FROM providers WHERE id=?", (ident,)).fetchone()
            if new_id and old:
                raise ValueError("连接标识发生冲突，请重新操作。")
            if provider_id is not None and not old:
                raise ValueError("要更新的供应商不存在。")
            if old and old[0] != protocol and db.execute("SELECT 1 FROM model_routes WHERE provider_id=?", (ident,)).fetchone():
                raise ValueError("此连接正在用于模型路由，不能直接变更接口类型。")
            if old and ciphertext is None and protocol != "codex_subscription":
                a, b = urlsplit(old[2]), urlsplit(endpoint)
                if old[0] != protocol or (a.scheme,a.hostname,a.port) != (b.scheme,b.hostname,b.port):
                    raise ValueError("变更接口域名或类型时请重新输入密钥。")
            encrypted = None if protocol == "codex_subscription" else ciphertext or (old[1] if old else None)
            if protocol != "codex_subscription" and encrypted is None:
                raise ValueError("该接口需要密钥。")
            # Freeze only receipts matching the COMPLETE pre-edit revision, before
            # any provider or child changes. A failed save rolls this back too.
            if old:
                self._freeze_capability_receipts(db, ident)
            db.execute("INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,protocol=excluded.protocol,base_url=excluded.base_url,model=excluded.model,encrypted_key=excluded.encrypted_key,updated_at=unixepoch()", (ident,name.strip(),protocol,endpoint,model,encrypted))
            for position, current in enumerate(selected):
                value = validated.get(current)
                if value is None:
                    item = db.execute("SELECT context_window,compact_limit,reasoning_levels,default_effort FROM connection_models WHERE provider_id=? AND model=?", (ident,current)).fetchone()
                    value = self._settings(item)
                db.execute("INSERT INTO connection_models VALUES (?,?,?,?,?,?,?) ON CONFLICT(provider_id,model) DO UPDATE SET position=excluded.position,context_window=excluded.context_window,compact_limit=excluded.compact_limit,reasoning_levels=excluded.reasoning_levels,default_effort=excluded.default_effort", (ident,current,position,value["context_window"],value["compact_limit"],json.dumps(value["reasoning_levels"]) if value["reasoning_levels"] is not None else None,value["default_effort"]))
            placeholders = ",".join("?" for _ in selected)
            db.execute("DELETE FROM connection_models WHERE provider_id=? AND model NOT IN ("+placeholders+")", (ident,*selected))
            db.execute("DELETE FROM model_probe_receipts WHERE provider_id=? AND model NOT IN ("+placeholders+")",(ident,*selected))
            # Legacy projection for old callers; never used for another child's settings.
            first = db.execute("SELECT context_window,compact_limit,reasoning_levels,default_effort FROM connection_models WHERE provider_id=? AND model=?", (ident,model)).fetchone()
            db.execute("INSERT OR REPLACE INTO model_settings VALUES (?,?,?,?,?)", (ident,*first))
            self._content_cache.pop(ident,None)
        return {"id":ident,"name":name.strip(),"protocol":protocol,"base_url":endpoint,"model":model,"models":selected,"has_key":encrypted is not None}

    @staticmethod
    def _validate_capability_effort(effort, channel):
        if not isinstance(channel, str) or channel not in CAPABILITY_CHANNELS:
            raise ValueError("能力证据路径无效。")
        if channel == "gemini_media" and effort is not None:
            raise ValueError("Gemini 媒体路径不设置思考强度。")
        if effort is not None and (not isinstance(effort, str) or effort not in EFFORTS):
            raise ValueError("能力证据的思考强度无效。")

    @staticmethod
    def _capability_identity(db, provider_id, model, effort, channel):
        row = db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?",
                         (provider_id,)).fetchone()
        if (not row or (channel == "gemini_media" and row[2] != "gemini_native")
                or not db.execute("SELECT 1 FROM connection_models WHERE provider_id=? AND model=?",
                                  (provider_id,model)).fetchone()):
            return None
        # DPAPI ciphertext is a credential VERSION, never decrypted. Re-entering
        # even the same key conservatively creates a new version. No raw key or
        # separate credential fingerprint is persisted in the evidence table.
        raw = json.dumps(["capability-v1", provider_id, model, row[2], row[3], effort, channel],
                         separators=(",", ":")).encode()
        token = hashlib.sha256(raw + b"\0" + (row[5] or b"")).hexdigest()
        return {"identity": token, "provider_id": provider_id, "model": model,
                "protocol": row[2], "base_url": row[3], "effort": effort, "channel": channel}

    def capability_identity(self, provider_id, model, effort=None, *, channel="connection", expected_identity=None):
        """Read one safe test-target snapshot; never resolve/decrypt credentials.

        None effort means unspecified, NOT the current default or any explicit
        effort. Pass the actual request effort. Keep this snapshot and supply it
        to record_capability_evidence after the test. expected_identity can also
        check it immediately before dispatch; this cannot lock a remote request.
        Missing targets and mismatches raise a fixed, non-sensitive error.
        """
        self._validate_capability_effort(effort, channel)
        with self.atomic() as db:
            identity = self._capability_identity(db, provider_id, model, effort, channel)
            if identity is None or (expected_identity is not None and identity != expected_identity):
                raise ValueError("能力测试目标不存在或身份已改变，请重新测试。")
            return identity

    def _freeze_capability_receipts(self, db, provider_id):
        row = db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?",
                         (provider_id,)).fetchone()
        if not row:
            return
        revision = self._content_revision(db, row)
        receipts = db.execute("SELECT model,effort,tested_at FROM model_probe_receipts "
                              "WHERE provider_id=? AND revision=? ORDER BY tested_at DESC,job_id DESC",
                              (provider_id,revision)).fetchall()
        for model, effort, tested_at in receipts:
            effort = effort or None
            if (effort is not None and effort not in EFFORTS
                    or type(tested_at) is not int or not 0 <= tested_at <= 253402300799):
                continue
            identity = self._capability_identity(db, provider_id, model, effort, "connection")
            if identity is not None:
                # No alias/job id/free text: old short probes prove connection only.
                db.execute("INSERT OR IGNORE INTO capability_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (provider_id,model,identity["identity"],"connection","connection",effort or "",
                            "passed",tested_at,"legacy_connection",None))

    def record_capability_evidence(self, provider_id, model, purpose, status, effort=None,
                                   expected_identity=None, *, channel="connection", tested_at=None,
                                   source="sample", error_code=None):
        """Store latest outcome for this exact target/purpose/channel/effort.

        expected_identity is the complete capability_identity snapshot. Omission
        binds to the current target (only suitable for synchronous local writes).
        Accept only fixed metadata; never pass samples, bodies or exception text.
        Errors describe test/transport problems, not a failed capability assertion.
        """
        self._validate_capability_effort(effort, channel)
        if not isinstance(purpose, str) or purpose not in CAPABILITY_PURPOSES:
            raise ValueError("能力证据用途无效。")
        if not isinstance(status, str) or status not in CAPABILITY_STATUSES:
            raise ValueError("能力证据状态无效。")
        if not isinstance(source, str) or source not in {"sample", "verified_task"}:
            raise ValueError("能力证据来源无效。")
        if (error_code is not None and (not isinstance(error_code, str)
                or error_code not in CAPABILITY_ERROR_CODES or status != "error")):
            raise ValueError("能力证据错误类别无效。")
        if tested_at is not None and (type(tested_at) is not int or not 0 <= tested_at <= 253402300799):
            raise ValueError("能力证据时间无效。")
        with self.atomic() as db:
            identity = self._capability_identity(db, provider_id, model, effort, channel)
            if identity is None or (expected_identity is not None and identity != expected_identity):
                raise ValueError("能力测试目标不存在或身份已改变，请重新测试。")
            self._freeze_capability_receipts(db, provider_id)
            if tested_at is None:
                tested_at = db.execute("SELECT unixepoch()").fetchone()[0]
            db.execute("INSERT INTO capability_evidence VALUES (?,?,?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(provider_id,model,identity,purpose,channel,effort) DO UPDATE SET "
                       "status=excluded.status,tested_at=excluded.tested_at,source=excluded.source,error_code=excluded.error_code "
                       "WHERE excluded.tested_at>=capability_evidence.tested_at",
                       (provider_id,model,identity["identity"],purpose,channel,effort or "",status,
                        tested_at,source,error_code or ("unknown" if status == "error" else None)))
        return self.capability_evidence(provider_id, model, channel=channel)

    def capability_evidence(self, provider_id, model, *, channel=None):
        """Return outcomes including stale history; never infer authorization.

        status=stale preserves original_status. Consumers must select the exact
        channel and effort (None is unspecified); error/stale is not failed.
        Reading lazily freezes only matching v2 legacy receipts transactionally.
        """
        if channel is not None and (not isinstance(channel, str) or channel not in CAPABILITY_CHANNELS):
            raise ValueError("能力证据路径无效。")
        with self.atomic() as db:
            self._freeze_capability_receipts(db, provider_id)
            rows = db.execute("SELECT identity,purpose,channel,effort,status,tested_at,source,error_code "
                              "FROM capability_evidence WHERE provider_id=? AND model=? AND (? IS NULL OR channel=?) "
                              "ORDER BY purpose,channel,effort,tested_at DESC,identity", (provider_id,model,channel,channel)).fetchall()
            result = []
            for token, purpose, row_channel, effort, status, tested_at, source, error_code in rows:
                current = self._capability_identity(db, provider_id, model, effort or None, row_channel)
                stale = current is None or current["identity"] != token
                result.append({"provider_id": provider_id, "model": model, "identity": token,
                               "purpose": purpose, "channel": row_channel, "status": "stale" if stale else status,
                               "original_status": status, "tested_at": tested_at, "effort": effort or None,
                               "source": source, "error_code": error_code})
            return result

    @staticmethod
    def _settings(row):
        return dict(zip(("context_window","compact_limit","reasoning_levels","default_effort"),
                        (row[0],row[1],json.loads(row[2]) if row[2] else None,row[3]) if row else (None,)*4))

    def all_model_settings(self, provider_id):
        with self.connection() as db:
            return {r[0]:self._settings(r[1:]) for r in db.execute(
                "SELECT model,context_window,compact_limit,reasoning_levels,default_effort FROM connection_models WHERE provider_id=?",(provider_id,))}

    def model_settings(self, provider_id, model=None):
        with self.connection() as db:
            row = db.execute("SELECT context_window,compact_limit,reasoning_levels,default_effort FROM connection_models WHERE provider_id=? AND model=COALESCE(?,(SELECT model FROM providers WHERE id=?))", (provider_id,model,provider_id)).fetchone()
        return self._settings(row)

    def model_ids(self, provider_id):
        with self.connection() as db:
            return [r[0] for r in db.execute("SELECT model FROM connection_models WHERE provider_id=? ORDER BY position,model", (provider_id,))]

    def list(self):
        with self.connection() as db:
            rows=db.execute("SELECT id,name,protocol,base_url,model,encrypted_key IS NOT NULL,EXISTS(SELECT 1 FROM disabled_connections d WHERE d.provider_id=p.id) FROM providers p ORDER BY updated_at DESC,id").fetchall()
            children={}
            for child in db.execute("SELECT provider_id,model,context_window,compact_limit,reasoning_levels,default_effort FROM connection_models ORDER BY position,model"):
                children.setdefault(child[0],{})[child[1]]=self._settings(child[2:])
            return [{"id":r[0],"name":r[1],"protocol":r[2],"base_url":r[3],"model":r[4],"models":list(children.get(r[0],{})),
                     "model_settings":children.get(r[0],{}),"has_key":bool(r[5]),"disabled":bool(r[6]),
                     **children.get(r[0],{}).get(r[4],self._settings(None))} for r in rows]

    def is_disabled(self, provider_id):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM disabled_connections WHERE provider_id=?", (provider_id,)).fetchone())

    def official_model_disabled(self, model):
        with self.connection() as db:
            return bool(db.execute("SELECT 1 FROM connection_models m JOIN providers p ON p.id=m.provider_id JOIN disabled_connections d ON d.provider_id=p.id WHERE p.protocol='codex_subscription' AND m.model=?", (model,)).fetchone())

    def enabled_subscription_models(self):
        with self.connection() as db:
            return {r[0] for r in db.execute("SELECT m.model FROM connection_models m JOIN providers p ON p.id=m.provider_id WHERE p.protocol='codex_subscription' AND NOT EXISTS(SELECT 1 FROM disabled_connections d WHERE d.provider_id=p.id)")}

    def enable_connection(self, provider_id):
        with self.atomic() as db:
            if not db.execute("SELECT 1 FROM providers WHERE id=?", (provider_id,)).fetchone():
                raise ValueError("连接不存在。")
            db.execute("DELETE FROM disabled_connections WHERE provider_id=?", (provider_id,))

    def get_secret(self, provider_id):
        with self.connection() as db:
            row = db.execute("SELECT encrypted_key FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not row or row[0] is None:
            raise ValueError("供应商没有保存密钥。")
        return _dpapi(row[0], decrypt=True).decode()

    def _read_profile(self, db, provider_id, model=None):
        row = db.execute("SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not row:
            return None
        current = model or row[4]
        if not db.execute("SELECT 1 FROM connection_models WHERE provider_id=? AND model=?", (provider_id,current)).fetchone():
            return None
        cached=getattr(self,"_content_cache",{}).get(provider_id) if getattr(self,"_transaction",None) is not None else None
        if cached is None:
            cached=(self._content_revision(db,row),[r[0] for r in db.execute("SELECT model FROM connection_models WHERE provider_id=? ORDER BY position,model",(provider_id,))])
            if getattr(self,"_transaction",None) is not None: self._content_cache[provider_id]=cached
        return {"id":row[0],"name":row[1],"protocol":row[2],"base_url":row[3],"model":current,"models":list(cached[1]),"has_key":row[5] is not None,"_revision":cached[0]},row[5]

    def resolve_profile(self, provider_id):
        with self.connection() as db:
            selected = self._read_profile(db,provider_id)
        if not selected:
            return None
        profile, encrypted = selected
        if encrypted is None:
            raise ValueError("供应商没有保存密钥。")
        return profile, _dpapi(encrypted, decrypt=True).decode()

    def resolve_media_capability(self, provider_id, model):
        """Capture profile, credential version and evidence identity together.

        Read under one SQLite transaction so even an A-B-A concurrent edit
        cannot pair one endpoint/key with another endpoint's identity. Only the
        captured ciphertext is decrypted, after the database snapshot closes.
        """
        with self.atomic() as db:
            selected = self._read_profile(db, provider_id, model)
            identity = self._capability_identity(db, provider_id, model, None, 'gemini_media')
            disabled = db.execute('SELECT 1 FROM disabled_connections WHERE provider_id=?', (provider_id,)).fetchone()
            if (not selected or selected[1] is None or not identity or disabled
                    or selected[0]['protocol'] != 'gemini_native'):
                raise ValueError('媒体连接已停用或不可用。')
            profile, encrypted = selected
        return profile, _dpapi(encrypted, decrypt=True).decode(), identity

    def set_route(self, alias, provider_id, model=None):
        if not isinstance(alias,str) or not re.fullmatch(r"[A-Za-z0-9_.:/~-]{1,160}",alias):
            raise ValueError("模型别名无效。")
        with self.atomic() as db:
            selected = self._read_profile(db,provider_id,model)
            if not selected or selected[0]["protocol"] not in MODEL_ADAPTERS:
                raise ValueError("此连接不能用作第三方模型路由。")
            db.execute("INSERT INTO model_routes(alias,provider_id,model) VALUES (?,?,?) ON CONFLICT(alias) DO UPDATE SET provider_id=excluded.provider_id,model=excluded.model", (alias,provider_id,selected[0]["model"]))

    def get_route(self, alias):
        with self.connection() as db:
            row = db.execute("SELECT provider_id,model FROM model_routes WHERE alias=?", (alias,)).fetchone()
            selected = self._read_profile(db,*row) if row else None
            if not selected:
                return None
            return {k:v for k,v in selected[0].items() if not k.startswith("_")}

    def resolve_route(self, alias):
        with self.connection() as db:
            row = db.execute("SELECT provider_id,model FROM model_routes WHERE alias=?", (alias,)).fetchone()
            selected = self._read_profile(db,*row) if row else None
            if not selected or self.is_disabled(row[0]):
                return None
        profile, encrypted = selected
        if encrypted is None or profile["protocol"] not in MODEL_ADAPTERS:
            raise ValueError("模型路由已失效。")
        return profile, _dpapi(encrypted,decrypt=True).decode()

    def bind_engine_route(self, alias, provider_id, revision, account, expected_settings=None, model=None):
        if (not isinstance(alias,str) or not re.fullmatch(r"[A-Za-z0-9_.:/~-]{1,160}",alias)
                or not valid_engine_account(account)):
            raise ValueError("模型线路标识无效。")
        with self.atomic() as db:
            selected = self._read_profile(db,provider_id,model)
            if not selected or selected[1] is None or selected[0]["_revision"] != revision:
                raise ValueError("连接在启用期间已改变，请重新选择。")
            if expected_settings is not None and self.model_settings(provider_id,model) != expected_settings:
                raise ValueError("模型设置在启用期间已改变，请重新选择。")
            self.set_route(alias,provider_id,selected[0]["model"])
            db.execute("INSERT INTO engine_bindings(alias,provider_id,revision,account,model) VALUES (?,?,?,?,?) ON CONFLICT(alias) DO UPDATE SET provider_id=excluded.provider_id,revision=excluded.revision,account=excluded.account,model=excluded.model", (alias,provider_id,revision,account,selected[0]["model"]))

    def get_engine_binding(self, alias):
        with self.connection() as db:
            row = db.execute("SELECT b.provider_id,b.model,b.revision,b.account FROM engine_bindings b JOIN model_routes r ON r.alias=b.alias AND r.provider_id=b.provider_id AND r.model=b.model WHERE b.alias=?", (alias,)).fetchone()
            selected = self._read_profile(db,row[0],row[1]) if row else None
            if not selected or selected[1] is None:
                return None
            profile = selected[0]
            if profile["_revision"] != row[2]:
                return None
            return dict(profile, _revision=row[2], account=row[3])

    def engine_aliases(self):
        with self.connection() as db:
            return [r[0] for r in db.execute("SELECT alias FROM engine_bindings ORDER BY alias") if self.get_engine_binding(r[0]) is not None]

    def known_engine_aliases(self):
        """Include stale and disabled aliases so they cannot masquerade as native models."""
        with self.connection() as db:
            return {r[0] for r in db.execute("SELECT alias FROM engine_bindings UNION SELECT alias FROM model_routes")}

    def set_jev_profile(self, provider_id):
        with self.atomic() as db:
            row = db.execute("SELECT protocol,encrypted_key FROM providers WHERE id=?", (provider_id,)).fetchone()
            if not row or row[0] != "system_one" or row[1] is None:
                raise ValueError("请选择已保存密钥的 Jev 连接。")
            db.execute("INSERT OR REPLACE INTO selections VALUES ('jev',?)", (provider_id,))
            self.enable_connection(provider_id)

    def get_jev_profile_id(self):
        with self.connection() as db:
            row = db.execute("SELECT s.provider_id FROM selections s JOIN providers p ON p.id=s.provider_id WHERE s.name='jev' AND p.protocol='system_one' AND p.encrypted_key IS NOT NULL AND NOT EXISTS(SELECT 1 FROM disabled_connections d WHERE d.provider_id=p.id)").fetchone()
            return row[0] if row else None

    def confirm_effort(self, alias, revision, effort):
        if effort not in EFFORTS:
            raise ValueError("推理强度无效。")
        with self.atomic() as db:
            route = self.get_route(alias)
            selected = self._read_profile(db,route["id"],route["model"]) if route else None
            if not selected or selected[0]["_revision"] != revision:
                raise ValueError("线路在验证期间已改变，请重新验证。")
            # Evidence is per model as well as per connection revision.
            db.execute("CREATE TABLE IF NOT EXISTS model_verified_efforts(provider_id TEXT, model TEXT, revision TEXT, effort TEXT, PRIMARY KEY(provider_id,model,revision,effort))")
            db.execute("INSERT OR IGNORE INTO model_verified_efforts VALUES (?,?,?,?)", (route["id"],route["model"],revision,effort))

    def confirmed_efforts(self, profile):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='model_verified_efforts'").fetchone():
                return set()
            return {r[0] for r in db.execute("SELECT effort FROM model_verified_efforts WHERE provider_id=? AND model=? AND revision=?", (profile["id"],profile["model"],profile["_revision"]))}
