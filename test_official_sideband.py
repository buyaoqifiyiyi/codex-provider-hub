"""Loopback regression tests for Codex's built-in image and search endpoints."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
from unittest.mock import patch

import zstandard

from front_proxy import make_front
from storage import OFFICIAL_CODEX_URL

TOKEN = "synthetic_local_capability_0123456789"
AUTH = "Bearer synthetic-chatgpt-auth"


class FakeStore:
    def engine_aliases(self):
        return []

    def official_model_disabled(self, _model):
        return False


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self.headers = {
            "Content-Type": "application/json",
            "x-codex-imagegen-request-id": "synthetic-image-id",
            "x-request-id": "synthetic-request-id",
        }
        self._body = io.BytesIO(body)

    def read(self, size=-1):
        return self._body.read(size)

    def close(self):
        self._body.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class OfficialSidebandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hub-sideband-test-")
        self.engine_home = Path(self.temp.name)
        self.config = {
            "defaultProvider": "openai",
            "providers": {"openai": {
                "adapter": "openai-responses",
                "baseUrl": OFFICIAL_CODEX_URL,
                "authMode": "forward",
                "codexAccountMode": "direct",
            }},
        }
        self.write_config()
        self.front = make_front(FakeStore(), TOKEN, 1, self.engine_home)
        self.thread = threading.Thread(target=self.front.serve_forever, daemon=True)
        self.thread.start()
        self.mock = patch("front_proxy.open_upstream", create=True)
        self.upstream = self.mock.start()
        self.upstream.side_effect = lambda *_: FakeResponse(
            b'{"created":1,"data":[{"b64_json":"c3ludGhldGlj"}]}'
        )

    def tearDown(self):
        self.mock.stop()
        self.front.shutdown()
        self.front.server_close()
        self.thread.join()
        self.temp.cleanup()

    def write_config(self):
        (self.engine_home / "config.json").write_text(json.dumps(self.config), encoding="utf-8")

    def request(self, path, body, *, token=TOKEN, auth=AUTH, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.front.server_port, timeout=5)
        request_headers = {
            "X-Provider-Hub-Token": token,
            "ChatGPT-Account-ID": "synthetic-account",
            "X-Codex-Image-Turn-Id": "synthetic-turn",
            "Content-Type": "application/json",
        }
        if auth is not None:
            request_headers["Authorization"] = auth
        request_headers.update(headers or {})
        conn.request("POST", path, body=body, headers=request_headers)
        response = conn.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        conn.close()
        return result

    def test_image_generation_preserves_json_auth_and_image_response(self):
        payload = {"model": "gpt-image-2", "prompt": "synthetic cover", "size": "auto"}
        raw = json.dumps(payload).encode()
        status, response_headers, body = self.request(
            "/v1/images/generations", raw,
            headers={"Host": "attacker.invalid", "Cookie": "synthetic-cookie"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["data"][0]["b64_json"], "c3ludGhldGlj")
        self.assertEqual(response_headers.get("x-codex-imagegen-request-id"), "synthetic-image-id")
        url, forwarded_body, headers = self.upstream.call_args.args
        self.assertEqual(url, OFFICIAL_CODEX_URL + "/images/generations")
        self.assertEqual(json.loads(forwarded_body), payload)
        headers = {key.lower(): value for key, value in headers.items()}
        self.assertEqual(headers["authorization"], AUTH)
        self.assertEqual(headers["chatgpt-account-id"], "synthetic-account")
        self.assertEqual(headers["x-codex-image-turn-id"], "synthetic-turn")
        self.assertNotIn("x-provider-hub-token", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("host", headers)

    def test_image_edit_preserves_reference_image_and_fixed_upstream(self):
        payload = {"model": "gpt-image-2", "prompt": "edit", "images": [
            {"image_url": "data:image/png;base64,c3ludGhldGlj"}]}
        status, _, body = self.request("/v1/images/edits", json.dumps(payload).encode())
        self.assertEqual(status, 200)
        self.assertIn(b"c3ludGhldGlj", body)
        url, raw, _ = self.upstream.call_args.args
        self.assertEqual(url, OFFICIAL_CODEX_URL + "/images/edits")
        self.assertEqual(json.loads(raw), payload)

    def test_builtin_search_without_model_is_forwarded(self):
        payload = {"search_query": [{"q": "synthetic"}]}
        status, _, _ = self.request("/v1/alpha/search", json.dumps(payload).encode())
        self.assertEqual(status, 200)
        url, raw, _ = self.upstream.call_args.args
        self.assertEqual(url, OFFICIAL_CODEX_URL + "/alpha/search")
        self.assertEqual(json.loads(raw), payload)

    def test_zstd_body_is_forwarded_uncompressed(self):
        raw = json.dumps({"model": "gpt-image-2", "prompt": "synthetic"}).encode()
        compressed = zstandard.ZstdCompressor().compress(raw)
        status, _, _ = self.request("/v1/images/generations", compressed,
                                    headers={"Content-Encoding": "zstd"})
        self.assertEqual(status, 200)
        _, body, headers = self.upstream.call_args.args
        self.assertEqual(body, raw)
        self.assertNotIn("content-encoding", {key.lower() for key in headers})

    def test_missing_local_or_official_auth_does_not_forward(self):
        body = b'{"model":"gpt-image-2","prompt":"synthetic"}'
        self.assertEqual(self.request("/v1/images/generations", b"", token="wrong")[0], 401)
        self.assertEqual(self.request("/v1/images/generations", body, auth=None)[0], 401)
        self.upstream.assert_not_called()

    def test_disabled_official_route_and_unknown_path_stay_blocked(self):
        self.config["providers"]["openai"]["disabled"] = True
        self.write_config()
        body = b'{"model":"gpt-image-2","prompt":"synthetic"}'
        self.assertEqual(self.request("/v1/images/generations", body)[0], 503)
        self.assertEqual(self.request("/v1/images/unknown", body)[0], 404)
        self.upstream.assert_not_called()

    def test_upstream_error_status_is_preserved_without_reflecting_body(self):
        self.upstream.side_effect = urllib.error.HTTPError(
            OFFICIAL_CODEX_URL, 429, "synthetic", {}, io.BytesIO(b"synthetic-secret")
        )
        status, _, body = self.request("/v1/images/generations", b'{"model":"gpt-image-2"}')
        self.assertEqual(status, 429)
        self.assertNotIn(b"synthetic-secret", body)

    def test_large_image_response_is_not_truncated(self):
        image = "x" * 1_500_000
        response_body = json.dumps({"created": 1, "data": [{"b64_json": image}]}).encode()
        self.upstream.side_effect = lambda *_: FakeResponse(response_body)
        status, _, body = self.request("/v1/images/generations", b'{"model":"gpt-image-2"}')
        self.assertEqual(status, 200)
        self.assertEqual(body, response_body)

    def test_redirect_is_not_reflected_or_followed(self):
        self.upstream.side_effect = urllib.error.HTTPError(
            OFFICIAL_CODEX_URL, 302, "redirect", {"Location": "https://attacker.invalid/"},
            io.BytesIO(b"synthetic-secret"),
        )
        status, headers, body = self.request("/v1/images/generations", b'{"model":"gpt-image-2"}')
        self.assertEqual(status, 502)
        self.assertNotIn("Location", headers)
        self.assertNotIn(b"synthetic-secret", body)
        self.upstream.assert_called_once()


if __name__ == "__main__":
    unittest.main()
