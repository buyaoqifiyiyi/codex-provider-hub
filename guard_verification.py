"""Fail-closed checks for the merged, trusted subagent guard."""
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading

PLUGIN = "codex-provider-hub@codex-provider-hub-local"
OLD_PLUGIN = "subagent-model-guard@subagent-model-guard-local"
SUFFIXES = {"pre_tool_use:0:0", "pre_tool_use:1:0", "post_tool_use:0:0", "subagent_start:0:0", "subagent_stop:0:0"}
REQUIRED_FILES = {"hooks/guard.cjs", "hooks/hooks.json", "lib/guard.cjs", "lib/model-catalog.cjs", "lib/disabled-models.cjs",
                  "lib/model-policy.cjs", "lib/parent-route.cjs", "lib/v2-task-recovery.cjs", "lib/auto-selection.cjs", "lib/state-persistence.cjs",
                  "lib/binding-kind.cjs", "lib/scheduling.cjs", "scripts/binding-kind.py", "scripts/scheduling.cjs",
                  "package.json", "policy.json", "storage.py", "native_catalog.py", "capability_service.py", "scripts/read-auto-signals.py", "scripts/schedule-state.py", "scripts/check-parent-route.py", "scripts/current-session.py",
                  "scripts/list-models.cjs", "scripts/parent-transcript.py",
                  "scripts/provider_hub_route.py", "scripts/read-model-catalog.py", "scripts/disabled-models.py", "scripts/v2-message-target.py"}


def select_desktop_executable(candidates, running):
    """Never silently verify against a different installed desktop version."""
    paths = {str(Path(path).resolve()).casefold(): Path(path) for path in candidates}
    active = {str(Path(path).resolve()).casefold() for path in running if path} & paths.keys()
    if len(active) == 1:
        return paths[next(iter(active))]
    if not active and len(paths) == 1:
        return next(iter(paths.values()))
    raise ValueError('desktop_codex_version_ambiguous')


def desktop_executable():
    root = Path(os.environ['LOCALAPPDATA']) / 'OpenAI' / 'Codex' / 'bin'
    candidates = [path for path in root.glob('*/codex.exe') if path.is_file()]
    if len(candidates) <= 1:
        return select_desktop_executable(candidates, [])
    # A desktop update can leave multiple binaries. Select only the one the
    # running desktop actually uses; multiple live versions remain ambiguous.
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    wmi = None
    try:
        wmi = win32com.client.GetObject('winmgmts:')
        running = [process.ExecutablePath for process in wmi.ExecQuery("SELECT ExecutablePath FROM Win32_Process WHERE Name='codex.exe'")]
        return select_desktop_executable(candidates, running)
    finally:
        wmi = None
        pythoncom.CoUninitialize()


def check_hooks(response: dict, installed: Path, inventory: dict, *, allow_old=False) -> bool:
    try:
        groups = response["data"]
        hooks = [hook for group in groups for hook in group.get("hooks", []) if hook.get("pluginId") == PLUGIN]
        old = [hook for group in groups for hook in group.get("hooks", []) if hook.get("pluginId") == OLD_PLUGIN and hook.get("enabled")]
        expected = {PLUGIN + ":hooks/hooks.json:" + suffix for suffix in SUFFIXES}
        if (old and not allow_old) or {hook.get("key") for hook in hooks} != expected or len(hooks) != len(expected): return False
        hook_path = (installed / "hooks/hooks.json").resolve()
        if any(not hook.get("enabled") or hook.get("trustStatus") != "trusted"
               or Path(hook.get("sourcePath", "")).resolve() != hook_path for hook in hooks): return False
        files = inventory.get("files")
        if inventory.get("version") != "0.6.6" or not isinstance(files, dict) or not REQUIRED_FILES.issubset(files): return False
        for name, digest in files.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts: return False
            path = installed / relative
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest: return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def query_host_hooks(codex_home: Path, cwd: Path) -> dict:
    env = dict(os.environ, CODEX_HOME=str(codex_home))
    process = subprocess.Popen([str(desktop_executable()), "app-server", "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    messages = queue.Queue()
    def read():
        for line in process.stdout:
            try: messages.put(json.loads(line))
            except ValueError: pass
    threading.Thread(target=read, daemon=True).start()
    def rpc(identifier, method, params):
        process.stdin.write(json.dumps({"id":identifier,"method":method,"params":params}) + "\n")
        process.stdin.flush()
        while True:
            value = messages.get(timeout=8)
            if value.get("id") == identifier:
                if "error" in value: raise ValueError("hook_query_failed")
                return value["result"]
    try:
        rpc(1,"initialize",{"clientInfo":{"name":"provider-hub-guard-check","version":"1"},
                            "capabilities":{"experimentalApi":True}})
        process.stdin.write(json.dumps({"method":"initialized"})+"\n"); process.stdin.flush()
        return rpc(2,"hooks/list",{"cwds":[str(cwd)]})
    finally:
        process.terminate()
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=3)
        process.stdin.close(); process.stdout.close()


def guard_verified(root: Path, codex_home: Path, cwd: Path | None = None, *, allow_old=False) -> bool:
    try:
        version = json.loads((Path(__file__).parent / '.codex-plugin' / 'plugin.json').read_text(encoding='utf-8'))['version']
        if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
            return False
        installed = codex_home / 'plugins/cache/codex-provider-hub-local/codex-provider-hub' / version
        inventory = json.loads((root/"guard-inventory.json").read_text(encoding="utf-8"))
        return check_hooks(query_host_hooks(codex_home, cwd or Path.cwd()), installed, inventory, allow_old=allow_old)
    except (OSError, ValueError, KeyError, TypeError, queue.Empty, subprocess.SubprocessError):
        return False
