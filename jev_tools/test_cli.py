import json
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch


CLI = Path(__file__).with_name("jev_cli.py")


def invoke(tool, raw):
    return subprocess.run([sys.executable, str(CLI), tool], input=raw, text=True,
                          encoding="utf-8", capture_output=True, timeout=15)


class CliTests(unittest.TestCase):
    def test_local_evidence_works_without_reloading_the_desktop(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "fixture.log").write_text("start\nERROR example failure\n", encoding="utf-8")
            call = invoke("jev_select_evidence", json.dumps({"root": root, "paths": ["fixture.log"], "query": "error"}))
        result = json.loads(call.stdout)
        self.assertEqual(call.returncode, 0)
        self.assertTrue(result["ok"])
        self.assertTrue(any("ERROR example failure" in x["text"] for x in result["result"]["snippets"]))
        self.assertFalse(result["result"]["remote"]["attempted"])

    def test_bad_json_is_rejected_without_echoing_input(self):
        call = invoke("jev_route_advice", '{"secret-marker":')
        self.assertEqual(call.returncode, 1)
        self.assertNotIn("secret-marker", call.stdout + call.stderr)
        self.assertFalse(json.loads(call.stdout)["ok"])

    def test_activity_status_is_available_without_a_model_request(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"CODEX_HOME": root}):
            call = invoke("jev_usage_status", "{}")
            result = json.loads(call.stdout)
            self.assertEqual(call.returncode, 0)
            self.assertEqual(result["result"]["status"], "no_records")
            self.assertFalse((Path(root) / "jev-activity.sqlite").exists())

    def test_utf8_chinese_input_and_output_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "fixture.txt").write_text("剪辑节奏与镜头动作\n", encoding="utf-8")
            payload = {"root": root, "paths": ["fixture.txt"], "query": "剪辑节奏"}
            call = invoke("jev_select_evidence", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(call.returncode, 0)
        result = json.loads(call.stdout)
        self.assertTrue(any("剪辑节奏与镜头动作" in item["text"] for item in result["result"]["snippets"]))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
    def test_cli_uses_selected_hub_credential_without_cc_switch(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_): pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body=json.dumps({'answers':{'ok':{'type':'noul','noul':0.9}}}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        service=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=service.serve_forever,daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix='jev-cli-hub-') as temp:
                root=Path(temp);local=root/'local';local.mkdir();hub=local/'CodexProviderHub';shared=hub/'shared';shared.mkdir(parents=True)
                source=Path(os.environ['LOCALAPPDATA'])/'CodexProviderHub'/'shared'
                for name in ('storage.py','jev_hub.py'):shutil.copyfile(source/name,shared/name)
                spec=importlib.util.spec_from_file_location('jev_cli_fixture_storage',shared/'storage.py')
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                store=module.ProfileStore(hub)
                profile=store.save('fixture','system_one',f'http://127.0.0.1:{service.server_port}/systemone','fixture-jev','synthetic-key')
                store.set_jev_profile(profile['id'])
                home=root/'codex';home.mkdir()
                with patch.dict(os.environ,{'CODEX_HOME':str(home),'LOCALAPPDATA':str(local)}):
                    call=invoke('jev_decide',json.dumps({'state':'fixture','questions':{'ok':{'type':'noul','instructions':'Is this a fixture?'}}}))
                self.assertEqual(call.returncode,0)
                self.assertTrue(json.loads(call.stdout)['ok'])
                self.assertNotIn('synthetic-key',call.stdout+call.stderr)
        finally:
            service.shutdown();service.server_close()


if __name__ == "__main__":
    unittest.main()
