"""Loopback and injected-upstream gateway tests; synthetic credentials only."""
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from gateway import make_server, subscription_history, safe_responses_stream
from responses_adapter import AdapterError, StreamError
from storage import ProfileStore

TOKEN = "local_synthetic_capability_0123456789"
ALIAS = "fixture-route/fixture-beta"
CODEX_AUTH = "Bearer synthetic-codex-account-token"
EXTERNAL_AUTH = "Bearer synthetic-external-provider-key"


class FakeResponse(io.BytesIO):
    status = 200
    def __init__(self, body, content_type="text/event-stream"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type, "x-codex-turn-state": "synthetic-sticky-state"}


class ResponsesStreamTests(unittest.TestCase):
    def test_done_after_completion_is_a_valid_terminator(self):
        raw=b'data: {"type":"response.completed","response":{"status":"completed"}}\n\ndata: [DONE]\n\n'
        self.assertEqual(b''.join(safe_responses_stream(io.BytesIO(raw))),raw)

    def test_done_before_terminal_does_not_hide_an_incomplete_stream(self):
        with self.assertRaises(StreamError):
            list(safe_responses_stream(io.BytesIO(b'data: [DONE]\n\n')))


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="provider-gateway-test-")
        from test_neutral_python_compat import isolated_native_catalog
        isolated_native_catalog(self, self.temp.name)
        self.store = ProfileStore(self.temp.name)
        provider = self.store.save("External", "chat_completions", "https://example.com/v1", "fixture-beta", "synthetic-external-provider-key")
        self.store.set_route(ALIAS, provider["id"])
        self.requests = []
        self.override = None
        def transport(url, data, headers):
            self.requests.append((url, json.loads(data), dict(headers)))
            if self.override is not None:
                return FakeResponse(*self.override)
            if url.endswith("/chat/completions"):
                chunks = [
                    {"id": "fixture", "object": "chat.completion.chunk", "created": 123, "model": "fixture-beta",
                     "choices": [{"index": 0, "delta": {"content": "synthetic reply"}, "finish_reason": "stop"}]},
                ]
                stream = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
                return FakeResponse(stream.encode())
            return FakeResponse(b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n')
        self.server = make_server(self.store, TOKEN, transport=transport, confirmed_efforts={ALIAS: {"high"}})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def request(self, payload, *, token=TOKEN, path="/v1/responses", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        opts = {"Content-Type": "application/json", "X-Provider-Hub-Token": token,
                "Authorization": CODEX_AUTH, "ChatGPT-Account-ID": "synthetic-account",
                "X-Sensitive-Other": "synthetic-foreign-header"}
        opts.update(headers or {})
        connection.request("POST", path, json.dumps(payload), opts)
        response = connection.getresponse()
        self.response_headers = dict(response.getheaders())
        result = response.status, response.read()
        connection.close()
        return result

    def test_subscription_never_uses_third_party_url(self):
        payload = {"model": "native-alpha", "input": "synthetic"}
        status, output = self.request(payload, headers={"Session_Id": "fixture-session",
                                                       "X-OpenAI-Account-Routing-Override": "fixture-route"})
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", output)
        url, forwarded, headers = self.requests[0]
        self.assertEqual(url, "https://chatgpt.com/backend-api/codex/responses")
        self.assertEqual(forwarded, payload)
        self.assertEqual(headers["Authorization"], CODEX_AUTH)
        self.assertEqual(headers["ChatGPT-Account-ID"], "synthetic-account")
        self.assertEqual(headers["Session_Id"], "fixture-session")
        self.assertEqual(headers["X-OpenAI-Account-Routing-Override"], "fixture-route")
        self.assertEqual(self.response_headers["x-codex-turn-state"], "synthetic-sticky-state")
        self.assertNotIn("X-Provider-Hub-Token", headers)
        self.assertNotIn("X-Sensitive-Other", headers)

    def test_external_route_receives_only_its_own_key_and_stream_is_translated(self):
        payload = {"model": ALIAS, "input": "synthetic", "stream": True,
                   "store": False, "reasoning": {"effort": "high"}}
        status, output = self.request(payload)
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", output)
        self.assertIn(b"synthetic reply", output)
        url, forwarded, headers = self.requests[0]
        self.assertEqual(url, "https://example.com/v1/chat/completions")
        self.assertEqual(forwarded["model"], "fixture-beta")
        self.assertEqual(forwarded["reasoning_effort"], "high")
        self.assertEqual(headers["Authorization"], EXTERNAL_AUTH)
        self.assertNotIn("ChatGPT-Account-ID", headers)
        self.assertNotIn("X-Provider-Hub-Token", headers)
        self.assertNotIn("X-Sensitive-Other", headers)
        self.assertNotIn(CODEX_AUTH, json.dumps(headers))

    def test_invalid_capability_unknown_route_and_opaque_history_fail_closed(self):
        self.assertEqual(self.request({"model": ALIAS, "input": "synthetic"}, token="wrong")[0], 401)
        self.assertEqual(self.request({"model": "missing-model", "input": "synthetic"})[0], 404)
        payload = {"model": ALIAS, "input": [{"type": "compaction", "encrypted_content": "opaque"}]}
        status, output = self.request(payload)
        self.assertEqual(status, 422)
        self.assertNotIn(b"opaque", output)
        self.assertEqual(self.requests, [])

    def test_native_model_cannot_be_hijacked_by_third_party_binding(self):
        provider = self.store.get_route(ALIAS)
        self.store.set_route("native-alpha", provider["id"])
        self.assertEqual(self.request({"model": "native-alpha", "input": "synthetic"})[0], 409)
        self.assertEqual(self.requests, [])

    def test_unknown_reasoning_effort_is_not_silently_downgraded(self):
        status, output = self.request({"model": ALIAS, "input": "synthetic", "reasoning": {"effort": "max"}})
        self.assertEqual(status, 422)
        self.assertIn(b"reasoning_effort_not_confirmed", output)
        self.assertEqual(self.requests, [])

    def test_third_party_error_body_and_event_are_not_forwarded(self):
        provider = self.store.save("external", "responses", "https://example.com/v1", "model", "fake-key")
        self.store.set_route("external-model", provider["id"])
        marker = "synthetic-secret-in-upstream-error"
        self.override = (json.dumps({"error": marker}).encode(), "application/json")
        status, output = self.request({"model": "external-model", "input": "fixture", "stream": True})
        self.assertEqual(status, 502)
        self.assertNotIn(marker.encode(), output)
        self.override = (("event: response.failed\ndata: " +
                          json.dumps({"type": "response.failed", "response": {"error": {"message": marker}}}) + "\n\n").encode(),
                         "text/event-stream")
        status, output = self.request({"model": "external-model", "input": "fixture", "stream": True})
        self.assertEqual(status, 200)
        self.assertIn(b"response.failed", output)
        self.assertNotIn(marker.encode(), output)

    def test_plaintext_reasoning_is_preserved_for_subscription_as_marked_history(self):
        body = {"model": "native-alpha", "input": [{"role": "user", "content": "before"},
            {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": None,
             "content": [{"type": "reasoning_text", "text": "synthetic reasoning"}]},
            {"role": "assistant", "content": "answer"}]}
        status, _ = self.request(body)
        self.assertEqual(status, 200)
        forwarded = self.requests[0][1]
        preserved = forwarded["input"][1]
        self.assertEqual((preserved["type"], preserved["role"]), ("message", "assistant"))
        self.assertIn("synthetic reasoning", preserved["content"][0]["text"])
        self.assertEqual(body["input"][1]["type"], "reasoning")
        unsafe = {"input": [{"type": "reasoning", "encrypted_content": "opaque",
                              "content": [{"type": "reasoning_text", "text": "fixture"}]}]}
        with self.assertRaises(AdapterError):
            subscription_history(unsafe)


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class RedirectTests(unittest.TestCase):
    def test_api_credential_is_never_followed_to_redirect_destination(self):
        seen = []
        class Destination(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                seen.append("hit")
                self.send_response(200); self.end_headers()
        destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                self.send_response(307)
                self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/stolen")
                self.send_header("Content-Length", "0"); self.end_headers()
        source = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for service in (source, destination):
            threading.Thread(target=service.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="provider-redirect-test-") as temp:
                store = ProfileStore(temp)
                profile = store.save("fixture", "responses", f"http://127.0.0.1:{source.server_port}/v1", "model", "synthetic-key")
                store.set_route("fixture-model", profile["id"])
                gateway = make_server(store, TOKEN)
                threading.Thread(target=gateway.serve_forever, daemon=True).start()
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=5)
                    conn.request("POST", "/v1/responses", json.dumps({"model": "fixture-model", "input": "synthetic"}),
                                 {"X-Provider-Hub-Token": TOKEN})
                    response = conn.getresponse()
                    self.assertEqual(response.status, 502)
                    response.read(); conn.close()
                    self.assertEqual(seen, [])
                finally:
                    gateway.shutdown(); gateway.server_close()
        finally:
            for service in (source, destination):
                service.shutdown(); service.server_close()


if __name__ == "__main__":
    unittest.main()
