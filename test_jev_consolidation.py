"""Exercise the bundled Jev tools through the actual MCP and a local fixture."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image

from install_local import JEV_SHARED, prepare_shared
from storage import ProfileStore

ROOT = Path(__file__).resolve().parent
JEV_TOOLS = {"jev_decide", "jev_route_advice", "jev_select_evidence",
             "jev_rank_transcripts", "jev_screen_frames", "jev_usage_status"}


def content(result):
    if result.is_error:
        raise AssertionError("MCP call failed")
    return result.structured_content or json.loads(next(x.text for x in result.content if x.type == "text"))


class Fixture(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.requests.append({"authorized": self.headers.get("Authorization") == "Bearer synthetic-key",
                              "images": bool(payload.get("assets"))})
        answers = {name: {"type": "noul", "noul": 0.8} for name in payload["questions"]}
        body = json.dumps({"answers": answers}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BundledJevTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_mcp_exposes_and_runs_original_tools(self):
        Fixture.requests = []
        fake = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        worker = threading.Thread(target=fake.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(fake.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(fake.shutdown)
        with tempfile.TemporaryDirectory(prefix="bundled-jev-") as temp:
            root = Path(temp)
            local = root / "local"
            home = root / "codex"
            store = ProfileStore(local / "CodexProviderHub")
            profile = store.save("fixture", "system_one", f"http://127.0.0.1:{fake.server_port}/systemone",
                                 "jev-fixture", "synthetic-key")
            store.set_jev_profile(profile["id"])
            image = root / "frame.png"
            Image.new("RGB", (320, 320), "red").save(image)
            log = root / "fixture.log"
            log.write_text("start\nERROR: synthetic evidence\n", encoding="utf-8")
            env = dict(os.environ, LOCALAPPDATA=str(local), CODEX_HOME=str(home),
                       HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
                       NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
            params = StdioServerParameters(command=os.sys.executable, args=[str(ROOT / "mcp_server.py")],
                                           env=env, cwd=str(ROOT))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertTrue(JEV_TOOLS <= names)
                    self.assertIn("list_providers", names)
                    result = content(await session.call_tool("jev_select_evidence", {
                        "root": str(root), "paths": ["fixture.log"], "query": "error"}))
                    self.assertTrue(any("synthetic evidence" in item["text"] for item in result["snippets"]))
                    self.assertFalse(Fixture.requests)
                    refused = await session.call_tool("jev_screen_frames", {
                        "frames": [{"id": "frame", "path": str(image), "time_seconds": 0}], "query": "red"})
                    self.assertTrue(refused.is_error)
                    self.assertFalse(Fixture.requests)
                    decision = content(await session.call_tool("jev_decide", {
                        "state": "synthetic facts", "questions": {"ok": {"type": "noul", "instructions": "fixture?"}}}))
                    self.assertEqual(decision["answers"]["ok"]["noul"], 0.8)
                    route = content(await session.call_tool("jev_route_advice", {
                        "task": "synthetic task", "facts": ["a bounded fixture"], "purpose": "subagent"}))
                    self.assertEqual(route["status"], "advisory")
                    frames = content(await session.call_tool("jev_screen_frames", {
                        "frames": [{"id": "frame", "path": str(image), "time_seconds": 0}],
                        "query": "red", "allow_external": True}))
                    self.assertEqual(frames["status"], "complete")
                    transcript = content(await session.call_tool("jev_rank_transcripts", {
                        "candidates": [{"id": "one", "start_seconds": 0, "end_seconds": 1, "transcript": "fixture"}],
                        "brief": "fixture", "allow_external": True}))
                    self.assertEqual(transcript["status"], "ok")
                    usage = content(await session.call_tool("jev_usage_status", {}))
                    self.assertNotEqual(usage["status"], "no_records")
                    self.assertTrue(all(item["authorized"] for item in Fixture.requests))
                    self.assertTrue(any(item["images"] for item in Fixture.requests))

    async def test_shared_compatibility_entry_uses_bundled_files(self):
        with tempfile.TemporaryDirectory(prefix="bundled-jev-shared-") as temp:
            root = Path(temp)
            prepare_shared(ROOT, root)
            prepare_shared(ROOT, root)
            shared = root / "shared"
            for name in JEV_SHARED:
                self.assertEqual((shared / "jev_tools" / name).read_bytes(), (ROOT / "jev_tools" / name).read_bytes())
            env = dict(os.environ, LOCALAPPDATA=str(root / "local"), CODEX_HOME=str(root / "codex"))
            params = StdioServerParameters(command=os.sys.executable, args=[str(shared / "jev_hub.py")],
                                           env=env, cwd=str(shared))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertTrue(JEV_TOOLS <= names)

    async def test_known_legacy_bridge_is_upgraded_after_bundling(self):
        with tempfile.TemporaryDirectory(prefix="bundled-jev-upgrade-") as temp:
            root = Path(temp)
            shared = root / "shared"
            shared.mkdir()
            current = (ROOT / "jev_hub.py").read_bytes()
            legacy_root = Path(temp) / "legacy-tools"
            old = ('JEV_ROOT = Path(r"' + str(legacy_root) + '")').encode()
            new = b'JEV_ROOT = Path(__file__).resolve().parent / "jev_tools"'
            (shared / "jev_hub.py").write_bytes(current.replace(new, old, 1))
            prepare_shared(ROOT, root)
            self.assertEqual((shared / "jev_hub.py").read_bytes(), current)
            self.assertTrue(all((shared / "jev_tools" / name).is_file() for name in JEV_SHARED))

    async def test_unknown_shared_bridge_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="bundled-jev-unknown-") as temp:
            root = Path(temp)
            shared = root / "shared"
            shared.mkdir()
            old = b"# independently customized bridge\n"
            (shared / "jev_hub.py").write_bytes(old)
            with self.assertRaises(ValueError):
                prepare_shared(ROOT, root)
            self.assertEqual((shared / "jev_hub.py").read_bytes(), old)
            self.assertFalse((shared / "jev_tools").exists())

    @unittest.skipUnless(os.environ.get("LOCALAPPDATA") and shutil.which("node"), "local runtime and Node required")
    async def test_portable_launcher_exposes_bundled_tools(self):
        runtime = Path(os.environ["LOCALAPPDATA"]) / "CodexProviderHub" / "runtime" / "Scripts" / "python.exe"
        if not runtime.is_file():
            self.skipTest("connection center runtime is not installed")
        with tempfile.TemporaryDirectory(prefix="bundled-jev-launcher-") as temp:
            env = dict(os.environ, CODEX_HOME=temp)
            params = StdioServerParameters(command="node", args=[str(ROOT / "scripts" / "start-provider-mcp.cjs")],
                                           env=env, cwd=str(ROOT))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    self.assertTrue(JEV_TOOLS <= names)


if __name__ == "__main__":
    unittest.main()
