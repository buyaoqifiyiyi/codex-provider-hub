"""Fetch the public CC Switch model reference without sending connection data."""
import json
import threading
import time
import urllib.error
import urllib.request

REFERENCE_URL = "https://models.dev/api.json"
MAX_BYTES = 16 * 1024 * 1024
SUCCESS_TTL = 60 * 60
FAILURE_TTL = 5 * 60
_lock = threading.Lock()
_cached = None
_expires = 0.0
_index_lock = threading.Lock()
_indexed_document = None
_index = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def load_catalog(*, opener=None, now=None):
    """Return the cached public catalog or None; no provider URL or key is sent."""
    global _cached, _expires
    moment = time.monotonic() if now is None else now
    with _lock:
        if opener is None and moment < _expires:
            return _cached
        client = opener or urllib.request.build_opener(_NoRedirect())
        try:
            request = urllib.request.Request(REFERENCE_URL, headers={"Accept": "application/json"}, method="GET")
            with client.open(request, timeout=2) as response:
                if getattr(response, "status", 200) != 200:
                    raise ValueError("reference_unavailable")
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("reference_too_large")
            document = json.loads(raw)
            if not isinstance(document, dict):
                raise ValueError("reference_invalid")
        except (urllib.error.URLError, OSError, UnicodeError, ValueError, TypeError):
            document = None
        if opener is None:
            _cached = document
            _expires = moment + (SUCCESS_TTL if document is not None else FAILURE_TTL)
        return document


def reference_for_models(base_url, model_ids, *, loader=None):
    """Resolve all requested IDs locally from one indexed public snapshot."""
    global _indexed_document, _index
    document = (loader or load_catalog)()
    if not isinstance(document, dict):
        with _index_lock:
            _indexed_document = None
            _index = None
        return {}
    from cc_switch_metadata import build_metadata_index, resolve_metadata
    with _index_lock:
        if _indexed_document is not document:
            _index = build_metadata_index(document)
            _indexed_document = document
        index = _index
    result = {}
    for model in model_ids:
        value = resolve_metadata(model, base_url, index=index)
        if any(value.get(field) is not None for field in ("context_window", "reasoning_levels", "supports_image")):
            result[model] = value
    return result
