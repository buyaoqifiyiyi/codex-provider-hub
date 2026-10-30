"""Prepare stable runtime modules and a reviewed guard inventory."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

SHARED = ("storage.py", "gateway.py", "responses_adapter.py", "thread_handoff.py", "front_proxy.py", "service.py", "ocx_service.py", "jev_hub.py", "catalog.py", "native_catalog.py")
JEV_SHARED = ("server.py", "jev_activity.py", "route_advice.py", "evidence_select.py",
              "frame_candidates.py", "video_candidates.py")


def prepare_shared(source: Path, root: Path):
    target = root / "shared"
    names = (*SHARED, *("jev_tools/" + name for name in JEV_SHARED))
    upgrades = {}
    for name in names:
        src, dst = source/name, target/name
        if not src.is_file() or src.is_symlink() or dst.is_symlink():
            raise ValueError("共享运行文件缺失或包含链接。")
        if dst.exists():
            before, after = dst.read_bytes(), src.read_bytes()
            if before == after:
                continue
            current = b'JEV_ROOT = Path(__file__).resolve().parent / "jev_tools"'
            matches = list(re.finditer(rb'^JEV_ROOT = Path\(r"[^"\r\n]+"\)(?=\r?$)', before, re.MULTILINE))
            migrated = before[:matches[0].start()] + current + before[matches[0].end():] if len(matches) == 1 else b''
            if name != "jev_hub.py" or migrated != after:
                raise ValueError("已有共享运行文件不同，未覆盖。")
            upgrades[name] = before
    target.mkdir(parents=True, exist_ok=True)
    (target / "jev_tools").mkdir(exist_ok=True)
    # Install dependencies before switching the legacy entry to the new path.
    for name in names:
        src, dst = source/name, target/name
        if not dst.exists():
            with dst.open("xb") as out, src.open("rb") as input_file:
                shutil.copyfileobj(input_file, out)
    for name, before in upgrades.items():
        dst = target/name
        if dst.read_bytes() != before:
            raise ValueError("共享运行文件在升级期间发生变化。")
        temporary = dst.with_name(dst.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as out:
                out.write((source/name).read_bytes())
            os.replace(temporary, dst)
        finally:
            if temporary.exists():
                temporary.unlink()


def record_guard_inventory(source: Path, installed: Path, root: Path):
    files = [source/"policy.json", source/"package.json", source/"storage.py", source/"capability_service.py", source/"native_catalog.py"]
    for directory in ("hooks", "lib", "scripts"):
        files.extend(path for path in (source/directory).rglob("*")
                     if path.is_file() and path.suffix in {".json", ".cjs", ".py"})
    hashes = {}
    for path in sorted(files):
        relative = path.relative_to(source).as_posix()
        copy = installed/relative
        if not copy.is_file() or copy.is_symlink() or copy.read_bytes() != path.read_bytes():
            raise ValueError("安装后的守卫与已审查源码不一致。")
        hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hashes:
        raise ValueError("没有找到已审查的守卫文件。")
    output = root/"guard-inventory.json"
    output.write_text(json.dumps({"version":"0.6.6","files":hashes},ensure_ascii=False,indent=2),encoding="utf-8")
    return len(hashes)
