import os
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import mcp_server
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MediaToolTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"id": "fixture-id", "name": "示例", "protocol": "gemini_native",
                        "model": "gemini-test", "models": ["gemini-test"], "disabled": False}

    def test_uses_only_enabled_native_connection_and_selected_model(self):
        with patch.object(mcp_server, "ProfileStore") as factory, patch.object(mcp_server, "inspect_media", return_value={"ok": True}) as inspect:
            store = factory.return_value
            store.list.return_value = [self.profile]
            store.resolve_media_capability.return_value = (self.profile, "synthetic-key", {"identity":"revision"})
            store.is_disabled.return_value = False
            result = mcp_server.analyze_gemini_media("C:/clip.mp4", "说明内容")
            self.assertTrue(result["ok"])
            inspect.assert_called_once_with(self.profile, "synthetic-key", "gemini-test", "C:/clip.mp4", "说明内容")
            self.assertNotIn("synthetic-key", str(result))

    def test_no_or_ambiguous_connection_does_not_transmit(self):
        with patch.object(mcp_server, "ProfileStore") as factory, patch.object(mcp_server, "inspect_media") as inspect:
            store = factory.return_value
            store.list.return_value = [{**self.profile, "disabled": True}]
            self.assertEqual(mcp_server.analyze_gemini_media("C:/clip.mp4", "看")["code"], "GEMINI_CONNECTION_MISSING")
            store.list.return_value = [self.profile, {**self.profile, "id": "other", "name": "另一个"}]
            self.assertEqual(mcp_server.analyze_gemini_media("C:/clip.mp4", "看")["code"], "GEMINI_CONNECTION_AMBIGUOUS")
            inspect.assert_not_called()

    def test_unknown_model_and_changed_connection_do_not_transmit(self):
        with patch.object(mcp_server, "ProfileStore") as factory, patch.object(mcp_server, "inspect_media") as inspect:
            store = factory.return_value
            store.list.return_value = [self.profile]
            store.resolve_media_capability.return_value = (self.profile, "synthetic-key", {"identity":"revision"})
            store.is_disabled.return_value = False
            self.assertEqual(mcp_server.analyze_gemini_media("C:/clip.mp4", "看", model="missing")["code"], "GEMINI_MODEL_UNAVAILABLE")
            store.capability_identity.side_effect = ValueError('changed')
            self.assertEqual(mcp_server.analyze_gemini_media("C:/clip.mp4", "看")["code"], "GEMINI_CONNECTION_CHANGED")
            inspect.assert_not_called()

    def test_tool_sends_media_to_synthetic_native_provider(self):
        received = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                received.append((self.path, self.headers.get("x-goog-api-key"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                data = json.dumps({"candidates": [{"content": {"parts": [{"text": "已看到视频"}]},
                                                    "finishReason": "STOP"}]}).encode("utf-8")
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)
        provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        worker = threading.Thread(target=provider.serve_forever, daemon=True); worker.start()
        self.addCleanup(provider.server_close); self.addCleanup(worker.join); self.addCleanup(provider.shutdown)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sample.mp4"; path.write_bytes(b"synthetic-video")
            profile = {**self.profile, "base_url": f"http://127.0.0.1:{provider.server_port}"}
            with patch.object(mcp_server, "ProfileStore") as factory, \
                 patch.object(mcp_server, "_start_media_sample", return_value=None), \
                 patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
                store = factory.return_value
                store.list.return_value = [profile]
                store.resolve_media_capability.return_value = (profile, "synthetic-key", {"identity":"revision"})
                store.is_disabled.return_value = False
                result = mcp_server.analyze_gemini_media(str(path), "描述视频")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["answer"], "已看到视频")
            self.assertEqual(received[0][0], "/v1beta/models/gemini-test:generateContent")
            self.assertEqual(received[0][1], "synthetic-key")
            self.assertEqual(received[0][2]["contents"][0]["parts"][1]["inlineData"]["mimeType"], "video/mp4")
            self.assertNotIn("synthetic-key", str(result))


class MediaToolDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_can_discover_media_tool(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(__file__).resolve().parent
            params = StdioServerParameters(command=sys.executable, args=[str(root / "mcp_server.py")], cwd=str(root),
                                           env=dict(os.environ, LOCALAPPDATA=temp, CODEX_HOME=str(Path(temp) / "codex")))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    self.assertIn("analyze_gemini_media", tools)
                    self.assertEqual(set(tools["analyze_gemini_media"].input_schema["required"]), {"file_path", "question"})


if __name__ == "__main__":
    unittest.main()
