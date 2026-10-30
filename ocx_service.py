"""Supervise the pinned OpenCodex engine behind an authenticated loopback front."""
from __future__ import annotations

import http.client
import json
import os
import secrets
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

from front_proxy import make_front
from service import _create_capability, _read_capability
from storage import ProfileStore, default_data_root, _dpapi

ENGINE_VERSION = "v2.93.0"
ENGINE_SHA256 = "c50f7b56306d32183a9ff6d23117a7759c395349aac3aec1f4387ca560ed5042"
KEYRING_SHA256 = "f0d154e418b8ab013a89e57216dbe75a3841cb8964c781d110f8d6f704529eab"


def _state(root: Path) -> dict:
    try:
        state = json.loads((root / "gateway-state.json").read_text(encoding="utf-8"))
        if (state.get("engine") != ENGINE_VERSION or type(state.get("port")) is not int
                or not 1 <= state["port"] <= 65535 or type(state.get("inner_port")) is not int
                or not 1 <= state["inner_port"] <= 65535):
            raise ValueError()
        return state
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError, AttributeError):
        raise ValueError("本地服务状态无效。") from None


def status(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else default_data_root()
    conn = None
    try:
        state, token = _state(root), _read_capability(root)
        port = state["port"]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        conn.request("GET", "/v1/_provider_hub/health", headers={"X-Provider-Hub-Token": token})
        response = conn.getresponse(); body = response.read(4097)
        value = json.loads(body)
        if response.status != 200 or len(body) > 4096 or value.get("service") != "codex-provider-hub" or value.get("version") != 1:
            raise ValueError()
        external = value.get("external_route_ready")
        if not isinstance(external, bool):
            # Read a single older readiness field without binding it to a provider.
            legacy = [v for k, v in value.items() if k.endswith("_route_ready") and k != "official_route_ready" and isinstance(v, bool)]
            external = legacy[0] if len(legacy) == 1 else False
        return {"running": True, "port": port, "official_route_ready": value.get("official_route_ready") is True,
                "external_route_ready": external}
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, http.client.HTTPException):
        return {"running": False, "official_route_ready": False, "external_route_ready": False}
    finally:
        if conn is not None: conn.close()


def _reserve_inner_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.2", 0))
        return sock.getsockname()[1]


def _choose_inner_port(previous: dict, config_path: Path) -> int:
    if previous.get("inner_port"):
        return previous["inner_port"]
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            port = config.get("port")
            if config.get("hostname") != "127.0.0.2" or type(port) is not int or not 1 <= port <= 65535:
                raise ValueError()
            return port
        except (OSError, ValueError, TypeError, AttributeError):
            raise ValueError("内置引擎的未完成初始化无法安全恢复。") from None
    return _reserve_inner_port()


def _initial_config(inner_port: int) -> dict:
    return {"port": inner_port, "hostname": "127.0.0.2", "defaultProvider": "openai",
            "openaiProviderTierVersion": 2, "syncResumeHistory": False,
            "clientIntegrations": {"codex": False, "grok": False, "claude-desktop": False},
            "claudeCode": {"enabled": False, "systemEnv": False},
            "providers": {"openai": {"adapter": "openai-responses", "baseUrl": "https://chatgpt.com/backend-api/codex",
                                      "authMode": "forward", "codexAccountMode": "direct"}}}



def _read_admin_capability(root: Path) -> str:
    token=_dpapi((Path(root)/"management-v2.bin").read_bytes(),decrypt=True).decode("ascii")
    if not token.startswith("ocx_admin_") or len(token)<40:
        raise ValueError("新版管理凭据无效。")
    return token


def _create_admin_capability(root: Path) -> str:
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/"management-v2.bin"
    if path.exists(): return _read_admin_capability(root)
    token="ocx_admin_"+secrets.token_urlsafe(32)
    encoded=_dpapi(token.encode("ascii"))
    # Service creation is serialized by gateway.lock; O_EXCL also prevents
    # an accidental concurrent creator from replacing a live management token.
    try:
        with path.open("xb") as output: output.write(encoded)
    except FileExistsError: return _read_admin_capability(root)
    return token


def _engine_environment(root: Path, token: str) -> dict:
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP", "NUMBER_OF_PROCESSORS",
               "PROCESSOR_ARCHITECTURE", "PROGRAMFILES", "PROGRAMFILES(X86)", "HTTP_PROXY", "HTTPS_PROXY"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    shadow = root / "shadow"
    for path in (shadow / "user", shadow / "appdata", shadow / "local", shadow / "codex", root / "engine-state"):
        path.mkdir(parents=True, exist_ok=True)
    env.update(USERPROFILE=str(shadow / "user"), HOME=str(shadow / "user"), APPDATA=str(shadow / "appdata"),
               LOCALAPPDATA=str(shadow / "local"), CODEX_HOME=str(shadow / "codex"),
               OPENCODEX_HOME=str(root / "engine-state"), OPENCODEX_API_AUTH_TOKEN=token,
               OPENCODEX_ADMIN_AUTH_TOKEN=_create_admin_capability(root),CI="1")
    inherited = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    env["NO_PROXY"] = ",".join(filter(None, (inherited, "127.0.0.1", "127.0.0.2", "localhost")))
    return env


def _runtime_matches(engine_home: Path, pid: int, inner_port: int) -> bool:
    try:
        runtime = json.loads((engine_home / "runtime-port.json").read_text(encoding="utf-8"))
        return runtime.get("pid") == pid and runtime.get("port") == inner_port
    except (OSError, ValueError, TypeError):
        return False


def serve(root: Path | None = None, *, executable: Path | None = None):
    if os.name != "nt":
        raise ValueError("此服务需要 Windows。")
    import hashlib
    import msvcrt
    import win32job
    root = Path(root) if root is not None else default_data_root()
    root.mkdir(parents=True, exist_ok=True)
    executable = Path(executable) if executable is not None else root / "engine-bin" / ENGINE_VERSION / "ocx.exe"
    with executable.open("rb") as binary:
        if hashlib.file_digest(binary, "sha256").hexdigest() != ENGINE_SHA256:
            raise ValueError("内置引擎完整性检查失败。")
    with (executable.parent / "keyring/keyring.win32-x64-msvc.node").open("rb") as binary:
        if hashlib.file_digest(binary, "sha256").hexdigest() != KEYRING_SHA256:
            raise ValueError("凭据组件完整性检查失败。")
    lock = open(root / "gateway.lock", "a+b")
    child = front = job = None
    try:
        if os.fstat(lock.fileno()).st_size == 0: lock.write(b"0"); lock.flush()
        lock.seek(0)
        try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError: return
        from catalog import recover_publication
        recover_publication(ProfileStore(root))
        token = _create_capability(root)
        previous = _state(root)
        engine_home = root / "engine-state"; engine_home.mkdir(parents=True, exist_ok=True)
        config_path = engine_home / "config.json"
        inner_port = _choose_inner_port(previous, config_path)
        if not config_path.exists():
            config_path.write_text(json.dumps(_initial_config(inner_port), ensure_ascii=False), encoding="utf-8")
        else:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if (config.get("hostname") != "127.0.0.2" or config.get("port") != inner_port
                    or config.get("syncResumeHistory") is not False
                    or config.get("claudeCode", {}).get("enabled") is not False
                    or config.get("claudeCode", {}).get("systemEnv") is not False
                    or any(config.get("clientIntegrations", {}).get(name) is not False for name in ("codex", "grok", "claude-desktop"))):
                raise ValueError("内置引擎配置不符合隔离要求。")
        def engine_ready():
            if not child or child.poll() is not None or not _runtime_matches(engine_home, child.pid, inner_port):
                return False
            try:
                with socket.create_connection(("127.0.0.2", inner_port), timeout=.1):
                    return True
            except OSError:
                return False
        front = make_front(ProfileStore(root), token, inner_port, engine_home,
                           port=previous.get("port", 0), engine_ready=engine_ready,
                           probe_capability=_create_admin_capability(root))
        child = subprocess.Popen([str(executable), "start", "--port", str(inner_port)], cwd=str(root),
                                 env=_engine_environment(root, token), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        job = win32job.CreateJobObject(None, "")
        information = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        information["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, information)
        win32job.AssignProcessToJobObject(job, int(child._handle))
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if child.poll() is not None: raise ValueError("内置引擎启动失败。")
            if engine_ready():
                try:
                    conn = http.client.HTTPConnection("127.0.0.2", inner_port, timeout=.3)
                    conn.request("GET", "/healthz"); response = conn.getresponse(); response.read(); conn.close()
                    if response.status == 200: break
                except (OSError, http.client.HTTPException): pass
            time.sleep(.1)
        else:
            raise ValueError("内置引擎启动超时。")
        state = {"version": 1, "engine": ENGINE_VERSION, "port": front.server_port, "inner_port": inner_port}
        temp = root / ("gateway-state." + str(os.getpid()) + ".tmp")
        temp.write_text(json.dumps(state), encoding="utf-8"); os.replace(temp, root / "gateway-state.json")
        threading.Thread(target=lambda: (child.wait(), front.shutdown()), daemon=True).start()
        front.serve_forever(poll_interval=.5)
        if child.poll() is not None:
            raise RuntimeError("内置引擎意外停止。")
    finally:
        if front is not None: front.server_close()
        if child is not None and child.poll() is None:
            child.terminate()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
        if job is not None: job.Close()
        lock.close()


def start(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else default_data_root()
    if (result := status(root))["running"]: return result
    if root.name != "CodexProviderHub": raise ValueError("本地服务目录无效。")
    root.mkdir(parents=True, exist_ok=True)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = root / "shared/ocx_service.py"
    if not launcher.is_file():
        raise ValueError("后台服务入口尚未准备好。")
    env = dict(os.environ, LOCALAPPDATA=str(root.parent))
    env.pop("PROVIDER_HUB_FIXTURE_ENGINE", None)
    subprocess.Popen([str(pythonw), str(launcher), "serve"], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        time.sleep(.1)
        if (result := status(root))["running"]: return result
    return {"running": False, "official_route_ready": False, "external_route_ready": False}


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] != "serve": raise SystemExit("Unsupported operation")
    fixture = os.environ.get("PROVIDER_HUB_FIXTURE_ENGINE")
    serve(executable=Path(fixture) if fixture else None)
