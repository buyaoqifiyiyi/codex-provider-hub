"""Offline Gemini backend and pinned-engine tests; synthetic credentials only."""
import http.client
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import probe
import mcp_server
import ocx_service
from storage import validate_provider_url
import test_anthropic as fixtures

KEY = "synthetic-gemini-secret"
ENGINE = Path(os.environ.get("LOCALAPPDATA", "")) / "CodexProviderHub" / "engine-bin" / "v2.93.0" / "ocx.exe"

class Reply(io.BytesIO):
    status = 200

def answer(text=probe.MARKER, reason="STOP"):
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": reason}]}

class GeminiProtocolTests(unittest.TestCase):
    def test_urls_auth_and_native_probe(self):
        seen = []
        def transport(url, data, headers):
            seen.append((url, json.loads(data), headers))
            return Reply(json.dumps(answer()).encode())
        for suffix in ("", "/", "/v1", "/v1beta/"):
            route = {"protocol": "gemini_native", "base_url": "https://fixture.invalid/relay" + suffix, "model": "gemini-fixture"}
            self.assertTrue(probe.probe_snapshot(route, KEY, None, transport=transport)["ok"])
        for url, body, headers in seen:
            self.assertEqual(url, "https://fixture.invalid/relay/v1beta/models/gemini-fixture:generateContent")
            self.assertEqual(headers["x-goog-api-key"], KEY)
            self.assertNotIn("Authorization", headers)
            self.assertNotIn(KEY, url + json.dumps(body))
            self.assertIn("contents", body)
        for bad in ("https://user:pass@fixture.invalid", "https://fixture.invalid?key=synthetic", "http://external.invalid", "https://fixture.invalid/../x"):
            with self.assertRaises(ValueError): validate_provider_url(bad, "gemini_native")

    def test_probe_rejects_incomplete_malformed_and_reasoning_only(self):
        route = {"protocol": "gemini_native", "base_url": "https://fixture.invalid", "model": "gemini-fixture"}
        cases = [(answer(reason="MAX_TOKENS"), "output_limit"), (answer(reason=""), "response_incomplete"),
                 (answer("other"), "marker_missing"), ({"error": {"message": KEY}}, "upstream_stream_error"),
                 ({"candidates": [None]}, "response_stream_invalid"), ([], "response_stream_invalid"),
                 ({"candidates": []}, "response_missing_completion")]
        thought = answer(); thought["candidates"][0]["content"]["parts"][0]["thought"] = True
        cases.append((thought, "marker_missing"))
        for value, code in cases:
            result = probe.probe_snapshot(route, KEY, None, transport=lambda *_: Reply(json.dumps(value).encode()))
            self.assertEqual(result.get("code"), code, result)
            self.assertNotIn(KEY, str(result))
        self.assertEqual(probe.probe_snapshot(route, KEY, "high")["code"], "unsupported_effort")

    def test_native_models_pagination_metadata_and_no_secrets(self):
        seen = []
        class Opener:
            def open(self, request, timeout):
                seen.append(request)
                row = {"models": [{"name": "models/gemini-a", "inputTokenLimit": 1000000,
                                   "supportedGenerationMethods": ["generateContent"]},
                                  {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
                                  {"name": "models/" + KEY}], "nextPageToken": "page/2"} if len(seen) == 1 else {"models": [{"name": "models/gemini-b"}]}
                return Reply(json.dumps(row).encode())
        result = probe.fetch_model_ids("https://fixture.invalid/v1", KEY, protocol="gemini_native", opener=Opener())
        self.assertEqual(result["models"], [{"id": "gemini-a", "context_window": 1000000}, {"id": "gemini-b"}])
        self.assertTrue(seen[1].full_url.endswith("?pageToken=page%2F2"))
        self.assertTrue(all(r.get_header("X-goog-api-key") == KEY for r in seen))
        self.assertNotIn(KEY, str(result))

    def test_native_errors_and_pagination_are_bounded_and_safe(self):
        route = {"protocol": "gemini_native", "base_url": "https://fixture.invalid", "model": "gemini-fixture"}
        def rejected(*_):
            raise urllib.error.HTTPError("https://fixture.invalid", 401, KEY, {}, Reply(json.dumps({"error": {"message": KEY}}).encode()))
        result = probe.probe_snapshot(route, KEY, None, transport=rejected)
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 401)
        self.assertNotIn(KEY, str(result))
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                return Reply(b'{"models":[{"name":"models/gemini-a"}],"nextPageToken":"same"}')
        opener = Opener()
        result = probe.fetch_model_ids(route["base_url"], KEY, protocol="gemini_native", opener=opener)
        self.assertTrue(result["truncated"])
        self.assertEqual(opener.calls, 2)

class GeminiSaveTests(unittest.TestCase):
    setUp = fixtures.ClaudeSaveTests.setUp
    tearDown = fixtures.ClaudeSaveTests.tearDown
    keyring = fixtures.ClaudeSaveTests.keyring
    write = fixtures.ClaudeSaveTests.write
    jobs = fixtures.ClaudeSaveTests.jobs
    start = fixtures.ClaudeSaveTests.start
    wait_ready = fixtures.ClaudeSaveTests.wait_ready
    authorize = fixtures.ClaudeSaveTests.authorize
    finish = fixtures.ClaudeSaveTests.finish

    def test_native_save_rename_and_only_added_model_is_tested(self):
        jobs = self.jobs()
        args = dict(protocol="gemini_native", base_url="https://fixture.invalid/v1beta", model="gemini-a", models=["gemini-a"], key=KEY)
        with patch("probe.probe_snapshot", return_value={"ok": True}) as checks, patch("probe.discover_capabilities", return_value={}):
            first = self.finish(jobs, self.start(jobs, **args))
            self.assertEqual(first["state"], "succeeded", first)
            ident = first["result"]["profile"]["id"]
            self.assertEqual(checks.call_count, 1)
            args.update(provider_id=ident, key="", name="Renamed")
            renamed = self.finish(jobs, self.start(jobs, **args))
            self.assertEqual(renamed["state"], "succeeded", renamed)
            self.assertEqual(checks.call_count, 1)
            args["models"] = ["gemini-a", "gemini-b"]
            added = self.finish(jobs, self.start(jobs, **args))
            self.assertEqual(added["state"], "succeeded", added)
            self.assertEqual(checks.call_count, 2)
            self.assertEqual(checks.call_args.args[0]["model"], "gemini-b")
        for alias in added["result"]["activation"]["aliases"]:
            self.assertTrue(__import__("front_proxy").named_route_ready(self.store, self.manager.home, alias))
            binding = self.store.get_engine_binding(alias)
            provider = self.config["providers"][binding["account"].split("/")[0]]
            self.assertEqual(provider["adapter"], "google")
            self.assertEqual(provider["googleMode"], "ai-studio")
            self.assertEqual(provider["baseUrl"], "https://fixture.invalid")
            self.assertTrue(provider["apiKey"].startswith("keychain:"))
        self.assertNotIn(KEY, json.dumps(added) + json.dumps(self.config))
        with patch("mcp_server.ProfileStore", return_value=self.store), patch("probe.fetch_model_ids", return_value={"ok": True, "models": [{"id": "gemini-a"}]}) as fetch:
            self.assertTrue(mcp_server.fetch_models("gemini_native", "https://fixture.invalid/v1", "", ident)["ok"])
            self.assertEqual(fetch.call_args.kwargs["protocol"], "gemini_native")
            self.assertFalse(mcp_server.fetch_models("gemini_native", "https://other.invalid", "", ident)["ok"])
            self.assertEqual(fetch.call_count, 1)

    def test_failed_native_probe_never_persists_or_activates(self):
        jobs = self.jobs()
        before = self.store.list()
        with patch("probe.probe_snapshot", return_value={"ok": False, "code": "marker_missing"}), patch("probe.discover_capabilities") as metadata:
            done = self.finish(jobs, self.start(jobs, protocol="gemini_native", base_url="https://fixture.invalid/v1",
                                              model="gemini-a", models=["gemini-a"], key=KEY))
        self.assertEqual(done["state"], "failed")
        self.assertEqual(self.store.list(), before)
        self.assertEqual(self.keys, {})
        metadata.assert_not_called()

@unittest.skipUnless(ENGINE.is_file(), "pinned fixture engine unavailable")
class GeminiEngineTests(unittest.TestCase):
    def test_native_text_tools_video_and_audio_boundary(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, body, self.headers.get("x-goog-api-key")))
                parts = [{"functionCall": {"name": "echo", "args": {"value": "fixture"}}}] if len(seen) == 2 else [{"text": "native fixture ok"}]
                value = {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}
                streaming = "streamGenerateContent" in self.path
                data = (("data: " + json.dumps(value) + "\n\n").encode() if streaming else json.dumps(value).encode())
                self.send_response(200); self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        fake = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=fake.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="hub-gemini-native-") as tmp:
                root = Path(tmp)
                with socket.socket() as sock:
                    sock.bind(("127.0.0.2", 0)); port = sock.getsockname()[1]
                env = ocx_service._engine_environment(root, "synthetic-local-admission")
                env.pop("HTTP_PROXY", None); env.pop("HTTPS_PROXY", None)
                config = ocx_service._initial_config(port)
                config["defaultProvider"] = "fixture"
                config["providers"] = {"fixture": {"adapter": "google", "googleMode": "ai-studio",
                    "baseUrl": f"http://127.0.0.1:{fake.server_port}", "allowPrivateNetwork": True,
                    "authMode": "key", "apiKey": KEY, "models": ["gemini-fixture"], "defaultModel": "gemini-fixture", "liveModels": False}}
                (root/"engine-state"/"config.json").write_text(json.dumps(config), encoding="utf-8")
                proc = subprocess.Popen([str(ENGINE), "start", "--port", str(port)], env=env, cwd=root,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                def request(body):
                    conn = http.client.HTTPConnection("127.0.0.2", port, timeout=15)
                    try:
                        conn.request("POST", "/v1/responses", json.dumps(dict(model="fixture/gemini-fixture", **body)),
                                     {"Content-Type": "application/json", "x-opencodex-api-key": "synthetic-local-admission"})
                        response = conn.getresponse(); return response.status, response.read().decode()
                    finally: conn.close()
                try:
                    for _ in range(200):
                        if proc.poll() is not None: self.fail("isolated engine exited")
                        try:
                            with socket.create_connection(("127.0.0.2", port), timeout=.1): break
                        except OSError: time.sleep(.05)
                    status, text = request({"input": "hello", "stream": False})
                    self.assertEqual(status, 200, text); self.assertIn("native fixture ok", text)
                    tools = [{"type": "function", "name": "echo", "description": "echo fixture", "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}]
                    status, text = request({"input": "call echo", "tools": tools, "stream": False})
                    self.assertEqual(status, 200, text)
                    result = json.loads(text)
                    call = next(row for row in result["output"] if row["type"] == "function_call")
                    status, text = request({"input": [{"role": "user", "content": "call echo"}, call,
                        {"type": "function_call_output", "call_id": call["call_id"], "output": "fixture result"}], "tools": tools, "stream": True})
                    self.assertEqual(status, 200, text); self.assertIn("response.completed", text)
                    self.assertIn("functionResponse", json.dumps(seen[-1][1]))
                    status, text = request({"input": [{"role": "user", "content": [
                        {"type": "input_text", "text": "describe"}, {"type": "input_video", "video_url": "data:video/mp4;base64,AAECAw=="}]}], "stream": False})
                    self.assertEqual(status, 200, text)
                    self.assertIn("AAECAw==", json.dumps(seen[-1][1])); self.assertIn("video/mp4", json.dumps(seen[-1][1]))
                    before = len(seen)
                    status, text = request({"input": [{"role": "user", "content": [
                        {"type": "input_audio", "audio_url": "data:audio/wav;base64,AAECAw==", "format": "wav"}]}], "stream": False})
                    self.assertEqual(status, 400, text)
                    self.assertIn("audio", text.lower()); self.assertEqual(len(seen), before)
                    self.assertTrue(all(key == KEY for _, _, key in seen))
                    self.assertTrue(all(path.startswith("/v1beta/models/gemini-fixture:") for path, _, _ in seen))
                finally:
                    if proc.poll() is None: proc.terminate(); proc.wait(timeout=8)
        finally:
            fake.shutdown(); fake.server_close()

if __name__ == "__main__": unittest.main()
