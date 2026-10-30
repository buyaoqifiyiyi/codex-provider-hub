"""Exercise only owned gateway processes against a disposable profile store."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from service import _read_capability, status
from storage import ProfileStore

SCRIPT = Path(__file__).resolve().with_name("service.py")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI and process lock are required")
class ServiceTests(unittest.TestCase):
    def test_owned_service_auth_single_instance_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="provider-service-test-") as temp:
            root = Path(temp) / "CodexProviderHub"
            store = ProfileStore(root)
            provider = store.save("External", "chat_completions", "https://example.invalid/v1", "fixture", "synthetic-key")
            snapshot,_=store.resolve_profile(provider['id'])
            store.bind_engine_route('fixture-route/fixture',provider['id'],snapshot['_revision'],
                                    'fixture-route/12345678-1234-1234-1234-123456789abc')
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), PYTHONDONTWRITEBYTECODE='1', LOCALAPPDATA=temp, CODEX_HOME=str(Path(temp)/"native-home"))
            def launch():
                return subprocess.Popen([sys._base_executable, "-B", str(SCRIPT), "serve"], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def ready():
                for _ in range(50):
                    result = status(root)
                    if result["running"]: return result
                    time.sleep(0.1)
                self.fail("owned service did not start")
            first = launch()
            second = None
            try:
                first_status = ready()
                self.assertTrue(first_status["external_route_ready"])
                token = _read_capability(root)
                self.assertNotIn(token.encode(), (root / "gateway-capability.bin").read_bytes())
                self.assertNotIn(token, json.dumps(first_status))
                connection = http.client.HTTPConnection("127.0.0.1", first_status["port"], timeout=2)
                connection.request("GET", "/v1/_provider_hub/health")
                self.assertEqual(connection.getresponse().status, 401)
                connection.close()
                duplicate = launch()
                self.assertEqual(duplicate.wait(timeout=8), 0)
                self.assertTrue(status(root)["running"])
                first.terminate(); first.wait(timeout=8)
                second = launch()
                self.assertEqual(ready()["port"], first_status["port"])
            finally:
                if first.poll() is None: first.terminate(); first.wait(timeout=8)
                if second is not None and second.poll() is None: second.terminate(); second.wait(timeout=8)


if __name__ == "__main__":
    unittest.main()
