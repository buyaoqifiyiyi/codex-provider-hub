"""Activate a saved connection in the embedded engine without plaintext config."""
from __future__ import annotations

import http.client
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import socket
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager, closing, nullcontext
from urllib.parse import urlsplit, quote

import ocx_service
from storage import MODEL_ADAPTERS, ProfileStore, default_data_root, effective_model_settings, valid_engine_account

NODE = Path(shutil.which("node") or "node")


def _requires_private_network(base_url: str) -> bool:
    url = urlsplit(base_url)
    if url.hostname in {"127.0.0.1", "::1", "localhost"}:
        return True
    if url.scheme != "https" or not url.hostname:
        return False
    try:
        result = queue.Queue(maxsize=1)
        def resolve():
            try: result.put(socket.getaddrinfo(url.hostname, url.port or 443, type=socket.SOCK_STREAM))
            except OSError: result.put([])
        threading.Thread(target=resolve, daemon=True).start()
        addresses = {ipaddress.ip_address(item[4][0]) for item in result.get(timeout=2)}
        fake_v4 = ipaddress.ip_network("198.18.0.0/15")
        fake_v6 = ipaddress.ip_network("2001:2::/48")
        def is_fake(address):
            return address in (fake_v4 if address.version == 4 else fake_v6)
        # Clash fake-IP answers are not the provider's actual destination. Keep
        # this opt-in scoped to the selected HTTPS endpoint and retain TLS checks.
        return bool(addresses) and any(is_fake(address) for address in addresses) and all(is_fake(address) or address.is_global for address in addresses)
    except (OSError, ValueError, queue.Empty):
        return False


class EngineManager:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "engine-state"

    def snapshot(self):
        return json.loads((self.home / "config.json").read_text(encoding="utf-8"))

    def replace_provider(self, name, provider):
        state = json.loads((self.root / "gateway-state.json").read_text(encoding="utf-8"))
        token = ocx_service._read_admin_capability(self.root)
        if not token.startswith("ocx_admin_") or type(state.get("inner_port")) is not int:
            raise ValueError("内置引擎管理状态不可用。")
        body = json.dumps({"name": name, "provider": provider}, ensure_ascii=False).encode()
        connection = http.client.HTTPConnection("127.0.0.2", state["inner_port"], timeout=8)
        try:
            connection.request("POST", "/api/providers", body,
                               {"Content-Type": "application/json", "Authorization": "Bearer " + token})
            response = connection.getresponse(); response.read(4096)
            return response.status == 200
        finally:
            connection.close()

    def delete_provider(self, name):
        state = json.loads((self.root / "gateway-state.json").read_text(encoding="utf-8"))
        token = ocx_service._read_admin_capability(self.root)
        if not token.startswith("ocx_admin_") or type(state.get("inner_port")) is not int:
            raise ValueError("内置引擎管理状态不可用。")
        connection = http.client.HTTPConnection("127.0.0.2", state["inner_port"], timeout=8)
        try:
            connection.request("DELETE", "/api/providers?name=" + quote(name, safe=""),
                               headers={"Authorization": "Bearer " + token})
            response = connection.getresponse(); response.read(4096)
            return response.status == 200
        finally:
            connection.close()


@contextmanager
def activation_lock(root: Path):
    import msvcrt
    root.mkdir(parents=True, exist_ok=True)
    with open(root / "activation.lock", "a+b") as lock:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0"); lock.flush()
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: raise ValueError("其他连接操作正在进行，请稍后重试。") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)



def _owned_engine_binding(store,db,provider_id,alias,account,current,allow_missing=False):
    if not valid_engine_account(account): return False
    row=db.execute("SELECT b.model,b.revision FROM engine_bindings b JOIN model_routes r ON r.alias=b.alias AND r.provider_id=b.provider_id AND r.model=b.model WHERE b.alias=? AND b.provider_id=? AND b.account=?",(alias,provider_id,account)).fetchone()
    selected=store._read_profile(db,provider_id,row[0]) if row else None
    if not selected or selected[0]["_revision"]!=row[1]: return False
    if current is None: return allow_missing
    profile=selected[0];model=row[0]
    return (isinstance(current,dict) and current.get("apiKey")=="keychain:"+account
            and current.get("adapter")==MODEL_ADAPTERS.get(profile["protocol"])
            and current.get("baseUrl","").rstrip("/")==profile["base_url"].rstrip("/")
            and current.get("defaultModel")==model and current.get("models")==[model])


def delete_provider(store: ProfileStore, provider_id: str, confirm_active: bool, manager, keyring_fn) -> dict:
    """Remove a connection and only engine credentials with a verified exact binding."""
    removed = []
    try:
        with closing(store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                profile = db.execute("SELECT protocol FROM providers WHERE id=?", (provider_id,)).fetchone()
                if not profile:
                    db.rollback()
                    return {"ok": False, "message": "连接不存在或已删除。"}
                routes = db.execute("SELECT alias FROM model_routes WHERE provider_id=?", (provider_id,)).fetchall()
                bindings = db.execute("SELECT alias,account FROM engine_bindings WHERE provider_id=?", (provider_id,)).fetchall()
                selections = db.execute("SELECT name FROM selections WHERE provider_id=?", (provider_id,)).fetchall()
                active = bool(routes or bindings or selections)
                if active and not confirm_active:
                    db.rollback()
                    return {"ok": False, "code": "connection_in_use", "message": "此连接正在使用，确认后才能删除。"}
                if bindings:
                    providers = manager.snapshot().get("providers", {})
                    for alias, account in bindings:
                        name = account.split("/", 1)[0]
                        current = providers.get(name)
                        other = db.execute("SELECT 1 FROM engine_bindings WHERE account=? AND provider_id<>?", (account, provider_id)).fetchone()
                        used_elsewhere = any(key != name and isinstance(value, dict) and value.get("apiKey") == "keychain:" + account
                                             for key, value in providers.items())
                        if (name == "openai" or not _owned_engine_binding(store,db,provider_id,alias,account,current,allow_missing=True)
                                or other or used_elsewhere):
                            raise ValueError("无法安全核对正在使用的模型线路。")
                        if any(item[0] == name for item in removed):
                            raise ValueError("模型线路存在重复绑定，无法安全删除。")
                        removed.append((name, current, account))
                    for name, current, account in removed:
                        if current is not None:
                            try:
                                manager.delete_provider(name)
                            except (OSError, ValueError, http.client.HTTPException):
                                pass  # A lost response is resolved through readback.
                            if name in manager.snapshot().get("providers", {}):
                                raise ValueError("无法停用当前模型线路，连接未删除。")
                        db.execute("INSERT OR IGNORE INTO pending_credential_cleanup(account) VALUES (?)", (account,))
                for table in ("model_routes", "engine_bindings", "selections", "verified_efforts", "model_settings", "disabled_connections", "connection_models", "model_verified_efforts", "model_probe_receipts"):
                    db.execute(f"DELETE FROM {table} WHERE provider_id=?", (provider_id,))
                db.execute("DELETE FROM providers WHERE id=?", (provider_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, http.client.HTTPException) as error:
        for name, previous, _ in removed:
            try:
                if previous and name not in manager.snapshot().get("providers", {}):
                    manager.replace_provider(name, previous)
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                pass
        return {"ok": False, "message": str(error) if isinstance(error, ValueError) else "删除未完成，请检查线路状态。"}
    pending = False
    for _, _, account in removed:
        try:
            if keyring_fn("discard", account, ""):
                with closing(store._connect()) as db, db:
                    db.execute("DELETE FROM pending_credential_cleanup WHERE account=?", (account,))
            else:
                pending = True
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            pending = True
    return {"ok": True, "credential_cleanup_pending": pending, "active_removed": active,
            "historical_credentials_untracked": profile[0] in MODEL_ADAPTERS,
            "official_settings_removed": profile[0] == "codex_subscription"}


def retry_credential_cleanup(store: ProfileStore, manager, keyring_fn) -> int:
    providers = manager.snapshot().get("providers", {})
    referenced = {value.get("apiKey", "").removeprefix("keychain:") for value in providers.values()
                  if isinstance(value, dict) and isinstance(value.get("apiKey"), str)
                  and value["apiKey"].startswith("keychain:")}
    with closing(store._connect()) as db:
        accounts = [row[0] for row in db.execute("SELECT account FROM pending_credential_cleanup")]
        referenced.update(row[0] for row in db.execute("SELECT account FROM engine_bindings"))
        for account in accounts:
            if account not in referenced and keyring_fn("discard", account, ""):
                with db:
                    db.execute("DELETE FROM pending_credential_cleanup WHERE account=?", (account,))
        return db.execute("SELECT count(*) FROM pending_credential_cleanup").fetchone()[0]


def disable_provider(store: ProfileStore, provider_id: str, manager) -> dict:
    """Pause a connection without deleting its profile, route or credential."""
    changed = []
    try:
        with closing(store._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                if not db.execute("SELECT 1 FROM providers WHERE id=?", (provider_id,)).fetchone():
                    db.rollback()
                    return {"ok": False, "message": "连接不存在。"}
                bindings = db.execute("SELECT alias,account FROM engine_bindings WHERE provider_id=?", (provider_id,)).fetchall()
                if bindings:
                    providers = manager.snapshot().get("providers", {})
                    for alias, account in bindings:
                        name = account.split("/", 1)[0]
                        current = providers.get(name)
                        other = db.execute("SELECT 1 FROM engine_bindings WHERE account=? AND provider_id<>?", (account, provider_id)).fetchone()
                        if (name == "openai" or not _owned_engine_binding(store,db,provider_id,alias,account,current)
                                or other):
                            raise ValueError("无法安全核对正在使用的模型线路。")
                        replacement = dict(current, disabled=True)
                        if current.get("disabled") is not True:
                            changed.append((name, current, replacement))
                            manager.replace_provider(name, replacement)
                            readback = manager.snapshot().get("providers", {}).get(name, {})
                            if readback.get("disabled") is not True or readback.get("apiKey") != current.get("apiKey"):
                                raise ValueError("停用模型线路失败。")
                db.execute("INSERT OR IGNORE INTO disabled_connections(provider_id) VALUES (?)", (provider_id,))
                db.execute("DELETE FROM selections WHERE name='jev' AND provider_id=?", (provider_id,))
                db.commit()
            except Exception:
                db.rollback()
                raise
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, http.client.HTTPException) as error:
        for name, previous, replacement in changed:
            try:
                current = manager.snapshot().get("providers", {}).get(name)
                if current == replacement:
                    manager.replace_provider(name, previous)
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
                pass
        return {"ok": False, "message": str(error) if isinstance(error, ValueError) else "停用未完成，请检查线路状态。"}
    return {"ok": True}


def keyring(action: str, account: str, secret: str, root: Path) -> bool:
    binding = root / "engine-bin" / ocx_service.ENGINE_VERSION / "keyring" / "keyring.win32-x64-msvc.node"
    with binding.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ocx_service.KEYRING_SHA256:
            raise ValueError("凭据组件完整性检查失败。")
    env = dict(os.environ, CODEX_PROVIDER_HUB_KEYRING=str(binding))
    process = subprocess.run([str(NODE), str(Path(__file__).with_name("keyring_helper.cjs"))],
                             input=json.dumps({"action": action, "account": account, "secret": secret}),
                             capture_output=True, text=True, encoding="utf-8", env=env, timeout=4,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return process.returncode == 0 and json.loads(process.stdout).get("ok") is True
    except ValueError:
        return False



class AliasValidationError(ValueError):
    def __init__(self,model):
        self.model=model
        super().__init__("模型 "+model+" 的路由别名过长或发生冲突，未发送短测试；请缩短模型 ID 或检查既有别名。")


def public_aliases(store,provider_id,models):
    with store.connection() as db:
        old=db.execute("SELECT alias,model FROM engine_bindings WHERE provider_id=?",(provider_id,)).fetchall()
    existing={model:alias for alias,model in old}
    result={};seen=set()
    for index,model in enumerate(models):
        name="hub-"+uuid.UUID(provider_id).hex+("-"+hashlib.sha256(model.encode()).hexdigest()[:12] if index else "")
        alias=existing.get(model,name+"/"+model)
        if len(alias)>160 or len(model)+34>160 or alias in seen:
            raise AliasValidationError(model)
        seen.add(alias);result[model]=alias
    return result


def activate_provider(store: ProfileStore, provider_id: str, manager: EngineManager, keyring_fn, *, expected_revision=None, publish=None, forward_deadline=None, prepared=None, commit_candidate=None, commit_guard=None, cancel_check=None, journal=None, on_commit=None) -> dict:
    """Stage every engine provider, then atomically publish the complete route set.

    The caller may wrap save+activation in store.atomic(). Engine/keychain writes
    have compensating rollback; no successful partial set is returned.
    """
    resolved = prepared[:2] if prepared else store.resolve_profile(provider_id)
    if not resolved:
        return {"ok": False, "code": "profile_missing"}
    profile, secret = resolved
    revision = profile["_revision"]
    if expected_revision is not None and revision != expected_revision:
        return {"ok": False, "code": "profile_changed"}
    if profile["protocol"] not in MODEL_ADAPTERS:
        return {"ok": False, "code": "model_profile_required"}
    forward_deadline=forward_deadline or (time.monotonic()+86400 if prepared else time.monotonic()+20)
    def check_deadline():
        if cancel_check: cancel_check()
        if time.monotonic() >= forward_deadline:
            raise ValueError("activation_deadline")
    before = manager.snapshot().get("providers", {})
    with store.connection() as db:
        old = db.execute("SELECT alias,account,model FROM engine_bindings WHERE provider_id=?", (provider_id,)).fetchall()
    aliases=public_aliases(store,provider_id,profile["models"])
    plans, written, touched = [], [], []
    catalog_path = store.root / "catalog.json"
    catalog_before = catalog_path.read_bytes() if publish and catalog_path.exists() else None
    def rollback_external():
        rollback_ok=True
        for name,expected in reversed(touched):
            try:
                current=manager.snapshot().get("providers",{}).get(name)
                # Never overwrite unrelated concurrent engine updates.
                owned=(isinstance(current,dict) and isinstance(expected,dict)
                       and current.get("apiKey")==expected.get("apiKey"))
                if not owned and current != expected and current != before.get(name):
                    rollback_ok=False; continue
                previous=before.get(name)
                if previous is None:
                    if current is not None: manager.delete_provider(name)
                else:
                    manager.replace_provider(name,previous)
                if manager.snapshot().get("providers",{}).get(name) != previous:
                    rollback_ok=False
            except (OSError,ValueError,KeyError,TypeError,http.client.HTTPException):
                rollback_ok=False
        if publish:
            try:
                if catalog_before is None:
                    catalog_path.unlink(missing_ok=True)
                else:
                    temp=catalog_path.with_suffix(".rollback.tmp")
                    temp.write_bytes(catalog_before); os.replace(temp,catalog_path)
            except OSError: rollback_ok=False
        for account in written:
            try:
                referenced=any(p.get("apiKey")=="keychain:"+account for p in manager.snapshot().get("providers",{}).values() if isinstance(p,dict))
                if not referenced:
                    if keyring_fn("discard",account,""):
                        (store.root/"staged-credentials"/(account.rsplit("/",1)[1]+".json")).unlink(missing_ok=True)
                        with store.atomic() as db:
                            db.execute("DELETE FROM pending_credential_cleanup WHERE account=?",(account,))
                            if db.execute("SELECT 1 FROM sqlite_master WHERE name='staged_engine_candidates'").fetchone():
                                db.execute("DELETE FROM staged_engine_candidates WHERE account=?",(account,))
                    else:
                        rollback_ok=False
                        with store.atomic() as db:
                            db.execute("INSERT OR IGNORE INTO pending_credential_cleanup VALUES (?)",(account,))
                else: rollback_ok=False
            except (OSError,ValueError,sqlite3.Error,http.client.HTTPException,subprocess.SubprocessError):
                rollback_ok=False
        return rollback_ok

    code = "activation_failed"
    try:
        check_deadline()
        private_network = _requires_private_network(profile["base_url"])
        for index, model in enumerate(profile["models"]):
            settings = prepared[2][model] if prepared else store.model_settings(provider_id, model)
            alias = aliases[model]
            if len(alias) > 160:
                code = "model_alias_too_long"; raise ValueError()
            name="hub-"+uuid.UUID(provider_id).hex[:16]+"-"+uuid.uuid4().hex[:12]
            account = name + "/" + str(uuid.uuid4())
            effective = effective_model_settings(settings)
            window, compact = effective["context_window"], effective["compact_limit"]
            provider = {"adapter":MODEL_ADAPTERS[profile["protocol"]],
                        "baseUrl":profile["base_url"], "authMode":"key", "apiKey":"keychain:"+account,
                         "models":[model], "defaultModel":model, "liveModels":False}
            if profile["protocol"] == "gemini_native":
                provider["googleMode"] = "ai-studio"
            for key,value in (("modelContextWindows",window),("modelAutoCompactTokenLimits",compact),
                              ("modelReasoningEfforts",effective["reasoning_levels"]),
                              ("modelDefaultReasoningEfforts",effective["default_effort"])):
                if value is not None:
                    provider[key] = {model:value}
            if private_network:
                provider["allowPrivateNetwork"] = True
            check_deadline()
            directory=store.root/"staged-credentials";directory.mkdir(parents=True,exist_ok=True)
            record=directory/(account.rsplit("/",1)[1]+".json")
            record.write_text(json.dumps({"name":name,"account":account}),encoding="utf-8")
            if journal: journal(name,account)
            written.append(account)
            code="keyring_write_failed"
            if not keyring_fn("write",account,secret): raise ValueError()
            plans.append((name,alias,account,model,settings,provider))
        names = [p[0] for p in plans]
        if len(set(names)) != len(names):
            code="model_alias_collision"; raise ValueError()
        for name,alias,account,model,settings,provider in plans:
            check_deadline()
            touched.append((name,provider))  # Includes lost responses after engine commit.
            if not manager.replace_provider(name,provider):
                code="engine_update_failed"; raise ValueError()
            current=manager.snapshot().get("providers",{}).get(name,{})
            if any(current.get(k) != v for k,v in provider.items()):
                code="engine_readback_failed"; raise ValueError()
        check_deadline()
        with (commit_guard() if commit_guard else nullcontext()), store.atomic() as db:
            if commit_candidate:
                commit_candidate()
                revision=store.resolve_profile(provider_id)[0]["_revision"]
            current=store.resolve_profile(provider_id)
            if not current or current[0]["_revision"] != revision:
                code="profile_changed"; raise ValueError()
            # Snapshot all aliases touched, including a previous owner, via SQLite rollback.
            db.execute("DELETE FROM model_routes WHERE provider_id=?",(provider_id,))
            db.execute("DELETE FROM engine_bindings WHERE provider_id=?",(provider_id,))
            for name,alias,account,model,settings,provider in plans:
                previous=db.execute("SELECT account FROM engine_bindings WHERE alias=?",(alias,)).fetchone()
                if previous:
                    db.execute("INSERT OR IGNORE INTO pending_credential_cleanup VALUES (?)",(previous[0],))
                    db.execute("INSERT OR REPLACE INTO retired_engine_providers VALUES (?,?)",(previous[0].split("/",1)[0],previous[0]))
                store.bind_engine_route(alias,provider_id,revision,account,expected_settings=settings,model=model)
                db.execute("DELETE FROM pending_credential_cleanup WHERE account=?",(account,))
            store.enable_connection(provider_id)
            from front_proxy import named_route_ready
            if any(not named_route_ready(store,manager.home,p[1]) for p in plans):
                code="route_readback_failed"; raise ValueError()
            for alias,account,model in old:
                db.execute("INSERT OR IGNORE INTO pending_credential_cleanup VALUES (?)",(account,))
                db.execute("INSERT OR REPLACE INTO retired_engine_providers VALUES (?,?)",(account.split("/",1)[0],account))
            if publish:
                code="catalog_update_failed"
                check_deadline()
                from catalog import transactional_publish
                transactional_publish(store,publish)
            check_deadline()
            if on_commit: on_commit(db,revision)
        if getattr(store,"_transaction",None) is not None:
            store._rollback_actions.append(rollback_external)
        for account in written:
            path=store.root/"staged-credentials"/(account.rsplit("/",1)[1]+".json")
            if getattr(store,"_transaction",None) is not None:
                store._commit_actions.append(lambda path=path:path.unlink(missing_ok=True))
            else: path.unlink(missing_ok=True)
        if not prepared and getattr(store,"_transaction",None) is None:
            try: cleanup_unpublished_providers(store,manager,keyring_fn)
            except (OSError,ValueError,sqlite3.Error,http.client.HTTPException,subprocess.SubprocessError): pass
        return {"ok":True,"profile_id":provider_id,"alias":plans[0][1],"aliases":[p[1] for p in plans],
                "catalog_updated":bool(publish)}
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error,http.client.HTTPException,subprocess.SubprocessError):
        rollback_ok=rollback_external()
        return {"ok":False,"code":code,"rollback_ok":rollback_ok}


def activate_local_provider(provider_id: str, *, pretested_revision: str | None = None) -> dict:
    """App-only entrypoint; callers receive metadata, never the secret."""
    import msvcrt
    root = default_data_root(); root.mkdir(parents=True, exist_ok=True)
    lock = open(root / "activation.lock", "a+b")
    try:
        if os.fstat(lock.fileno()).st_size == 0: lock.write(b"0"); lock.flush()
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: return {"ok": False, "code": "activation_busy"}
        if not ocx_service.start(root)["running"]:
            return {"ok": False, "code": "engine_not_running"}
        store = ProfileStore(root)
        selected = store.resolve_profile(provider_id)
        if not selected:
            return {"ok": False, "code": "profile_missing"}
        profile, secret = selected
        if profile["protocol"] not in MODEL_ADAPTERS:
            return {"ok": False, "code": "model_profile_required"}
        from probe import probe_snapshot, describe_probe_failure
        from probe import bounded_read as _bounded_read
        SAVE_READ_SECONDS = 15
        read_deadline=time.monotonic()+SAVE_READ_SECONDS
        for model in profile["models"]:
            check=({"ok":True} if pretested_revision == profile["_revision"] else _bounded_read(lambda m=model:probe_snapshot(dict(profile,model=m),secret,None),read_deadline))
            if not check.get("ok"):
                result={"ok":False,"code":"probe_failed","failed_model":model,"detail":check.get("code","unknown"),
                        "message":model+"："+describe_probe_failure(check)+"原有线路未更改。"}
                for field in ("http_status", "upstream_error_type", "request_id"):
                    if field in check: result[field]=check[field]
                return result
        from catalog import refresh
        result=activate_provider(store,provider_id,EngineManager(root),
                                lambda action,account,value:keyring(action,account,value,root),
                                expected_revision=profile["_revision"],publish=lambda:refresh(root,store=store))
        return result

    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return {"ok": False, "code": "activation_failed"}
    finally:
        lock.close()


def cleanup_unpublished_providers(store,manager,keyring_fn):
    with store.connection() as db:
        retired=db.execute("SELECT name,account FROM retired_engine_providers").fetchall()
        candidates=db.execute("SELECT name,account FROM staged_engine_candidates").fetchall() if db.execute("SELECT 1 FROM sqlite_master WHERE name='staged_engine_candidates'").fetchone() else []
    files=[]
    for file in (store.root/"staged-credentials").glob("*.json"):
        try:
            item=json.loads(file.read_text(encoding="utf-8"))
            if valid_engine_account(item.get("account")) and item.get("name")==item["account"].split("/",1)[0]:
                files.append((item["name"],item["account"]))
        except (OSError,ValueError,KeyError,TypeError): continue
    for name,account in dict(retired+candidates+files).items():
        with store.connection() as db:
            referenced=db.execute("SELECT 1 FROM engine_bindings WHERE account=?",(account,)).fetchone()
        if referenced: continue
        current=manager.snapshot().get("providers",{}).get(name)
        if current is not None:
            if current.get("apiKey")!="keychain:"+account: continue
            try: manager.delete_provider(name)
            except (OSError,ValueError,http.client.HTTPException): pass
            if name in manager.snapshot().get("providers",{}): continue
        with store.atomic() as db:
            db.execute("DELETE FROM retired_engine_providers WHERE name=? AND account=?",(name,account))
            if candidates: db.execute("DELETE FROM staged_engine_candidates WHERE name=? AND account=?",(name,account))
            db.execute("INSERT OR IGNORE INTO pending_credential_cleanup VALUES (?)",(account,))
    pending=retry_credential_cleanup(store,manager,keyring_fn)
    for name,account in files:
        with store.connection() as db:
            tracked=db.execute("SELECT 1 FROM pending_credential_cleanup WHERE account=?",(account,)).fetchone()
        if not tracked and name not in manager.snapshot().get("providers",{}):
            (store.root/"staged-credentials"/(account.rsplit("/",1)[1]+".json")).unlink(missing_ok=True)
    return pending
