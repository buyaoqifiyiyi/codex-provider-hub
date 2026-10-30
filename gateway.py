"""Experimental loopback Responses router. Not installed or enabled by default.

The caller owns service lifecycle and a per-install capability token. The router
never stores or prints incoming ChatGPT authorization, follows redirects, or
passes that authorization to a third-party route.
"""
from __future__ import annotations

import hmac
import json
from copy import deepcopy
import re
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from responses_adapter import AdapterError, StreamError, convert_stream, translate_request
from storage import MODEL_ADAPTERS, OFFICIAL_CODEX_URL
from native_catalog import native_model_ids

MAX_REQUEST = 32 * 1024 * 1024
MAX_SSE_FRAME = 8 * 1024 * 1024
OFFICIAL_HEADERS = {
    "authorization", "chatgpt-account-id", "openai-beta", "originator", "user-agent",
    "session-id", "session_id", "thread-id", "x-openai-subagent", "x-client-request-id",
    "x-codex-turn-state", "x-codex-turn-metadata", "x-codex-beta-features",
    "x-codex-routing-hint", "x-oai-attestation", "x-codex-installation-id",
    "x-codex-parent-thread-id", "x-openai-account-routing-override",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_upstream(url, data, headers):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=120)


def safe_responses_stream(response):
    """Forward valid event frames, replacing third-party error payloads."""
    frame, size, terminal_seen = [], 0, False
    while True:
        line = response.readline(MAX_SSE_FRAME + 1)
        if not line:
            if frame:
                raise StreamError("upstream_stream_truncated")
            return
        size += len(line)
        if size > MAX_SSE_FRAME:
            raise StreamError("upstream_event_too_large")
        if line.strip(b"\r\n"):
            frame.append(line)
            continue
        if not frame:
            size = 0
            continue
        data = []
        event_name = ""
        try:
            for part in frame:
                decoded = part.decode("utf-8").rstrip("\r\n")
                field, _, value = decoded.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    data.append(value)
                elif field == "event":
                    event_name = value
            if not data:
                # Heartbeat comments may be passed without inspecting bodies.
                if all(part.startswith(b":") for part in frame):
                    yield b": keepalive\n\n"
                    frame, size = [], 0
                    continue
                raise ValueError()
            if "\n".join(data).strip() == "[DONE]":
                if not terminal_seen:
                    raise StreamError("done_before_terminal")
                yield b"".join(frame) + b"\n"
                return
            payload = json.loads("\n".join(data))
            if not isinstance(payload, dict):
                raise ValueError()
            nested = payload.get("response")
            if (event_name == "error" or payload.get("type") in {"error", "response.failed"}
                    or payload.get("error") is not None
                    or isinstance(nested, dict) and nested.get("error") is not None):
                raise StreamError("upstream_error_event")
            if payload.get("type") in {"response.completed", "response.incomplete"}:
                terminal_seen = True
        except StreamError:
            raise
        except (ValueError, UnicodeError, json.JSONDecodeError):
            raise StreamError("invalid_upstream_event") from None
        yield b"".join(frame) + b"\n"
        frame, size = [], 0


def subscription_history(body: dict) -> tuple[dict, bool]:
    """Represent plaintext third-party reasoning as marked assistant history.

    The official endpoint does not accept nonempty reasoning.content from Chat
    providers. We preserve every plaintext segment as ordinary assistant text;
    encrypted/mixed or unknown forms are never guessed or discarded.
    """
    items = body.get("input")
    if not isinstance(items, list):
        return body, False
    copied = None
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("type") != "reasoning" or not item.get("content"):
            continue
        if (set(item) - {"type", "id", "content", "summary", "encrypted_content", "status"}
                or item.get("encrypted_content") not in (None, "")
                or item.get("summary") not in (None, [])
                or item.get("status") not in (None, "completed")
                or not isinstance(item["content"], list)):
            raise AdapterError("reasoning_history_not_supported")
        segments = []
        for part in item["content"]:
            if (not isinstance(part, dict) or set(part) - {"type", "text"}
                    or part.get("type") != "reasoning_text" or not isinstance(part.get("text"), str)):
                raise AdapterError("reasoning_history_not_supported")
            segments.append(part["text"])
        if copied is None:
            copied = deepcopy(body)
        copied["input"][index] = {"type": "message", "role": "assistant", "phase": "commentary",
                                  "content": [{"type": "output_text", "text":
                                               "[Prior third-party model reasoning, preserved for continuity]\n" + "".join(segments)}]}
    return (copied, True) if copied is not None else (body, False)


def make_server(store, token: str, *, host="127.0.0.1", port=0,
                transport=open_upstream, confirmed_efforts=None, chat_options=None, on_event=None):
    if host != "127.0.0.1" or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("invalid local gateway binding or capability")
    capabilities = confirmed_efforts or {}
    compatibility = chat_options or {}

    def record(kind, code=""):
        if on_event:
            try:
                on_event({"kind": kind, "code": code})
            except Exception:
                pass

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _authorized(self):
            supplied = self.headers.get("X-Provider-Hub-Token", "")
            return supplied.isascii() and hmac.compare_digest(supplied, token)

        def do_GET(self):
            if not self._authorized():
                self._error(401, "local_access_denied")
                return
            if self.path != "/v1/_provider_hub/health":
                self._error(404, "unsupported_endpoint")
                return
            ready = any((route := store.get_route(alias)) and not store.is_disabled(route["id"])
                        and route["protocol"] in MODEL_ADAPTERS for alias in store.known_engine_aliases())
            payload = json.dumps({"service": "codex-provider-hub", "version": 1,
                                  "external_route_ready": bool(ready)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
            self.close_connection = True

        def _error(self, status, code):
            record("rejected", code)
            body = json.dumps({"error": {"message": code, "code": code}}, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_POST(self):
            if not self._authorized():
                self._error(401, "local_access_denied")
                return
            if self.path not in {"/v1/responses", "/v1/responses/compact"}:
                self._error(404, "unsupported_endpoint")
                return
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                self._error(400, "invalid_request_length")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
                if not 0 < length <= MAX_REQUEST:
                    raise ValueError()
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError()
                body = json.loads(raw)
                model = body.get("model") if isinstance(body, dict) else None
                if not isinstance(model, str) or not model:
                    raise ValueError()
            except (ValueError, UnicodeError, json.JSONDecodeError):
                self._error(400, "invalid_request")
                return

            try:
                native = native_model_ids(root=store.root) | store.enabled_subscription_models()
                known = model in store.known_engine_aliases()
                if model in native and known:
                    self._error(409, "model_route_ambiguous")
                    return
                resolved = None if model in native else store.resolve_route(model)
            except (ValueError, UnicodeError):
                self._error(400, "invalid_route_or_request")
                return
            if model in native:
                if store.official_model_disabled(model):
                    self._error(503,"model_route_not_ready")
                    return
                route, third_party_key = {"protocol": "codex_subscription", "base_url": OFFICIAL_CODEX_URL}, None
            elif resolved is not None:
                route, third_party_key = resolved
            else:
                self._error(404, "model_route_not_found")
                return
            protocol = route["protocol"]
            headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
            if protocol == "codex_subscription":
                # This path is deliberately fixed; the subscription token cannot
                # be redirected by editing a provider's configured endpoint.
                url = OFFICIAL_CODEX_URL + self.path.removeprefix("/v1")
                for key, value in self.headers.items():
                    if key.lower() in OFFICIAL_HEADERS:
                        headers[key] = value
                if not any(key.lower() == "authorization" and value for key, value in headers.items()):
                    self._error(401, "subscription_auth_missing")
                    return
                try:
                    compatible, changed = subscription_history(body)
                    outbound = json.dumps(compatible, ensure_ascii=False, allow_nan=False).encode("utf-8") if changed else raw
                    if changed:
                        record("history_adapted", "plaintext_reasoning_as_assistant_text")
                except (AdapterError, ValueError, TypeError):
                    self._error(422, "reasoning_history_not_supported")
                    return
            elif protocol in {"responses", "chat_completions"}:
                if self.path != "/v1/responses":
                    self._error(501, "third_party_compaction_not_supported")
                    return
                if body.get("stream", True) is not True:
                    self._error(422, "third_party_stream_required")
                    return
                try:
                    headers["Authorization"] = "Bearer " + third_party_key
                    if protocol == "chat_completions":
                        url = route["base_url"] + "/chat/completions"
                        settings = compatibility.get(model, {})
                        efforts = capabilities[model] if model in capabilities else store.confirmed_efforts(route)
                        payload = translate_request(body, route["model"],
                                                    confirmed_reasoning_efforts=efforts,
                                                    disabled_hosted_tools=settings.get("disabled_hosted_tools", ()),
                                                    allow_custom_grammar_downgrade=settings.get("allow_custom_grammar_downgrade", False))
                    else:
                        url = route["base_url"] + "/responses"
                        payload = dict(body, model=route["model"])
                    outbound = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                except AdapterError as error:
                    self._error(422, error.code)
                    return
                except (ValueError, TypeError):
                    self._error(400, "invalid_route_or_request")
                    return
            else:
                self._error(400, "route_protocol_not_supported")
                return
            try:
                response = transport(url, outbound, headers)
                record("upstream_connected")
            except urllib.error.HTTPError as error:
                # Never reflect upstream bodies or redirect destinations into logs/chat.
                status = error.code if 400 <= error.code <= 599 else 502
                self._error(status, "upstream_rejected_request")
                return
            except (OSError, TimeoutError, ValueError):
                self._error(502, "upstream_unavailable")
                return
            try:
                with response:
                    status = getattr(response, "status", 200)
                    if status != 200:
                        self._error(status if 400 <= status <= 599 else 502, "upstream_rejected_request")
                        return
                    content_type = response.headers.get("Content-Type", "")
                    if protocol != "codex_subscription" and content_type.split(";", 1)[0].strip().lower() != "text/event-stream":
                        self._error(502, "upstream_content_type_invalid")
                        return
                    self.send_response(200)
                    content_type = content_type or "text/event-stream"
                    self.send_header("Content-Type", content_type)
                    if protocol == "codex_subscription":
                        turn_state = response.headers.get("x-codex-turn-state")
                        if turn_state:
                            self.send_header("x-codex-turn-state", turn_state)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    if protocol == "chat_completions":
                        for chunk in convert_stream(iter(response.readline, b""), model):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        record("stream_finished")
                    elif protocol == "responses":
                        for chunk in safe_responses_stream(response):
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    else:
                        while chunk := response.readline():
                            self.wfile.write(chunk)
                            self.wfile.flush()
            except AdapterError as error:
                record("adapter_failed", error.code)
                # Once streaming has begun, a fixed failure event is the only
                # safe outcome; never invent a successful completed response.
                try:
                    failure = {"type": "response.failed", "response": {"status": "failed",
                               "error": {"code": "upstream_stream_invalid", "message": "upstream_stream_invalid"}}}
                    self.wfile.write(("event: response.failed\ndata: " + json.dumps(failure) + "\n\n").encode())
                except (OSError, BrokenPipeError):
                    pass
            except (OSError, BrokenPipeError):
                pass

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        def handle_error(self, request, client_address):
            # Base HTTP server tracebacks may otherwise include request data.
            pass

    return Server((host, port), Handler)
