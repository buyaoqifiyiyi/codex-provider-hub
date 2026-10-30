"""Stdin JSON fallback for Jev tools when an existing Codex session caches old MCP tools."""
import argparse
import json
from pathlib import Path
import sys

import server


TOOLS = {
    "jev_decide": server.jev_decide,
    "jev_route_advice": server.jev_route_advice,
    "jev_select_evidence": server.jev_select_evidence,
    "jev_rank_transcripts": server.jev_rank_transcripts,
    "jev_screen_frames": server.jev_screen_frames,
    "jev_usage_status": server.jev_usage_status,
}
MAX_INPUT_BYTES = 1024 * 1024


def use_connection_center_if_configured():
    """Use the connection center's selected Jev profile."""
    bundled_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(bundled_root))
    from jev_hub import JevCredentials
    from storage import ProfileStore
    credentials = JevCredentials(ProfileStore())
    server.read_settings = credentials.read_settings
    server.api_key = credentials.api_key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=sorted(TOOLS))
    args = parser.parse_args()
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        use_connection_center_if_configured()
        raw = sys.stdin.read(MAX_INPUT_BYTES + 1)
        if len(raw.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ValueError("输入超过 1 MiB。")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("标准输入必须是 JSON 对象。")
        server._CALL_SOURCE.set("cli")
        result = TOOLS[args.tool](**payload)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as error:
        # Report the error category without echoing arbitrary input or credentials.
        print(json.dumps({"ok": False, "error_type": type(error).__name__}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
