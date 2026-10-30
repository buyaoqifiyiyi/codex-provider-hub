"""Persistent loopback gateway lifecycle; no user settings are changed here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request

from gateway import make_server
from storage import ProfileStore, _dpapi, default_data_root

def _read_capability(root: Path) -> str:
    return _dpapi((root / "gateway-capability.bin").read_bytes(), decrypt=True).decode("ascii")


def _create_capability(root: Path) -> str:
    path = root / "gateway-capability.bin"
    try:
        return _read_capability(root)
    except FileNotFoundError:
        token = secrets.token_urlsafe(32)
        encrypted = _dpapi(token.encode("ascii"))
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return _read_capability(root)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encrypted)
        return token


def _read_port(root: Path) -> int:
    try:
        state = json.loads((root / "gateway-state.json").read_text(encoding="utf-8"))
        port = state["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError()
        return port
    except FileNotFoundError:
        return 0
    except (ValueError, KeyError, TypeError):
        raise ValueError("本地服务状态损坏，未启动新的线路。") from None


def status(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else default_data_root()
    try:
        token, port = _read_capability(root), _read_port(root)
        if not port:
            raise ValueError()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/_provider_hub/health",
                                         headers={"X-Provider-Hub-Token": token})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1.5) as response:
            body = json.load(response)
        if body.get("service") != "codex-provider-hub" or body.get("version") != 1:
            raise ValueError()
        return {"running": True, "port": port, "external_route_ready": body.get("external_route_ready") is True}
    except (OSError, ValueError, UnicodeError, urllib.error.URLError):
        return {"running": False, "external_route_ready": False}


def serve(root: Path | None = None):
    if os.name != "nt":
        raise ValueError("此服务需要 Windows。")
    import msvcrt
    root = Path(root) if root is not None else default_data_root()
    root.mkdir(parents=True, exist_ok=True)
    # Only one service instance can own the current user's gateway state.
    lock = open(root / "gateway.lock", "a+b")
    try:
        if os.fstat(lock.fileno()).st_size == 0:
            lock.write(b"0"); lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        token = _create_capability(root)
        port = _read_port(root)
        server = make_server(ProfileStore(root), token, port=port)
        temp = root / ("gateway-state." + str(os.getpid()) + ".tmp")
        temp.write_text(json.dumps({"port": server.server_port, "version": 1}), encoding="utf-8")
        os.replace(temp, root / "gateway-state.json")
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
    finally:
        lock.close()


def start(root: Path | None = None) -> dict:
    root = Path(root) if root is not None else default_data_root()
    if (result := status(root))["running"]:
        return result
    root.mkdir(parents=True, exist_ok=True)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise ValueError("本地运行环境缺少 pythonw.exe。")
    env = dict(os.environ, LOCALAPPDATA=str(root.parent))
    subprocess.Popen([str(pythonw), str(Path(__file__).resolve()), "serve"], env=env,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))
    for _ in range(30):
        time.sleep(0.1)
        if (result := status(root))["running"]:
            return result
    return {"running": False, "external_route_ready": False}


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "serve":
        serve()
    else:
        raise SystemExit("Unsupported operation")
