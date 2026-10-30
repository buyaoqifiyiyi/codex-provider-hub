"""CC Switch model-list compatibility rules, adapted for the Hub's MCP runtime.

Adapted from CC Switch v4.0.21 src-tauri/src/services/model_fetch.rs,
copyright (c) 2025 Jason Young, MIT. See third_party/cc-switch/LICENSE.
"""
import re
from urllib.parse import urlsplit


# Keep the upstream order: the longest compatibility suffix must match first.
COMPAT_SUFFIXES = (
    "/api/claudecode", "/api/anthropic", "/apps/anthropic", "/api/coding",
    "/claudecode", "/anthropic", "/step_plan", "/coding", "/claude",
)


def model_url_candidates(base_url: str) -> list[str]:
    """Keep the Hub's existing first choice, then try CC Switch's same-origin paths."""
    base = base_url.rstrip("/")
    parsed = urlsplit(base)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}))
            or any(part in {".", ".."} for part in parsed.path.split("/"))):
        raise ValueError("模型列表地址无效。")
    candidates = [base + "/models"]
    if not re.search(r"/v\d+$", parsed.path):
        candidates.append(base + "/v1/models")
    elif not parsed.path.endswith("/v1"):
        candidates.append(base + "/v1/models")
    for suffix in COMPAT_SUFFIXES:
        if parsed.path.endswith(suffix):
            root = base[:-len(suffix)].rstrip("/")
            candidates.extend((root + "/v1/models", root + "/models"))
            if suffix == "/apps/anthropic":
                candidates.append(root + "/compatible-mode/v1/models")
            break
    origin = (parsed.scheme, parsed.hostname, parsed.port)
    result = []
    for candidate in candidates:
        target = urlsplit(candidate)
        if (target.scheme, target.hostname, target.port) != origin:
            raise ValueError("模型列表地址与连接来源不一致。")
        if candidate not in result:
            result.append(candidate)
    return result


def model_id(entry, *, catalog=False):
    """Extract supported OpenAI/CC Switch catalog IDs without accepting other data."""
    if isinstance(entry, str):
        return entry
    if not isinstance(entry, dict):
        return None
    for field in (("slug", "id") if catalog else ("id", "slug")):
        value = entry.get(field)
        if isinstance(value, str) and value:
            return value
    name = entry.get("name")
    return name[len("models/"):] if isinstance(name, str) and name.startswith("models/") else None
