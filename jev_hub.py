"""Reuse the existing Jev MCP through a Hub profile; not enabled by default."""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import sys
from contextlib import closing

from storage import ProfileStore, _dpapi

JEV_ROOT = Path(__file__).resolve().parent / "jev_tools"


class JevCredentials:
    def __init__(self, store: ProfileStore, profile_id: str | None = None):
        self.store = store
        self.profile_id = profile_id

    @staticmethod
    def _fingerprint(profile_id, endpoint, model):
        return hashlib.sha256("\0".join((profile_id, endpoint, model)).encode()).hexdigest()

    def read_settings(self):
        selected = self.profile_id or self.store.get_jev_profile_id()
        profile = next((p for p in self.store.list() if p["id"] == selected), None)
        if not profile or profile["protocol"] != "system_one" or not profile["has_key"] or profile["disabled"]:
            raise ValueError("Jev 连接尚未配置。")
        fingerprint = self._fingerprint(profile["id"], profile["base_url"], profile["model"])
        return {"provider_name": "hub:" + profile["id"] + ":" + fingerprint,
                "endpoint": profile["base_url"], "model": profile["model"]}

    def api_key(self, reference):
        if not isinstance(reference, str) or not reference.startswith("hub:"):
            raise ValueError("Jev 凭据引用无效。")
        parts = reference.split(":")
        if len(parts) != 3 or (self.profile_id and parts[1] != self.profile_id):
            raise ValueError("Jev 凭据引用无效。")
        profile_id = parts[1]
        with closing(self.store._connect()) as db:
            row = db.execute("SELECT protocol,base_url,model,encrypted_key FROM providers p WHERE id=? AND NOT EXISTS(SELECT 1 FROM disabled_connections d WHERE d.provider_id=p.id)",
                             (profile_id,)).fetchone()
        if not row or row[0] != "system_one" or row[3] is None:
            raise ValueError("Jev 连接不存在或密钥不可用。")
        fingerprint = self._fingerprint(profile_id, row[1], row[2])
        if not hmac.compare_digest(reference, "hub:" + profile_id + ":" + fingerprint):
            raise ValueError("Jev 连接已更新，请重试。")
        return _dpapi(row[3], decrypt=True).decode("utf-8")


def load_jev(credentials: JevCredentials, root: Path = JEV_ROOT):
    """Load existing Jev tools but replace CC Switch access with Hub access."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec = importlib.util.spec_from_file_location("provider_hub_existing_jev", root / "server.py")
    if spec is None or spec.loader is None:
        raise ValueError("找不到现有 Jev 服务。")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.read_settings = credentials.read_settings
    module.api_key = credentials.api_key
    module._CALL_SOURCE.set("mcp")
    return module


def main():
    profile_id = os.environ.get("PROVIDER_HUB_JEV_PROFILE", "")
    load_jev(JevCredentials(ProfileStore(), profile_id or None)).server.run(transport="stdio")


if __name__ == "__main__":
    main()
