"""Start and stop only owned embedded engine processes in temporary homes."""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import closing

from ocx_service import _choose_inner_port, status

ROOT = Path(__file__).resolve().parent
ENGINE = Path(os.environ.get('LOCALAPPDATA', ''))/'CodexProviderHub'/'engine-bin'/'v2.93.0'/'ocx.exe'


@unittest.skipUnless(os.name == 'nt' and ENGINE.is_file(), 'verified Windows fixture engine unavailable')
class EmbeddedServiceTests(unittest.TestCase):
    def test_interrupted_first_start_reuses_the_prepared_engine_port(self):
        with tempfile.TemporaryDirectory(prefix='hub-port-recovery-') as temp:
            config = Path(temp)/'config.json'
            config.write_text(json.dumps({'hostname':'127.0.0.2','port':24567}),encoding='utf-8')
            self.assertEqual(_choose_inner_port({}, config),24567)
            config.write_text(json.dumps({'hostname':'0.0.0.0','port':24567}),encoding='utf-8')
            with self.assertRaises(ValueError): _choose_inner_port({}, config)
    def test_start_duplicate_stop_and_restart_keep_a_stable_authenticated_port(self):
        with tempfile.TemporaryDirectory(prefix='provider-ocx-service-') as tmp:
            data = Path(tmp)/'CodexProviderHub'
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), PYTHONDONTWRITEBYTECODE='1', LOCALAPPDATA=tmp, CODEX_HOME=str(Path(tmp)/'native-home'), PROVIDER_HUB_FIXTURE_ENGINE=str(ENGINE))
            def launch():
                # Launch the owned process directly while processing the venv's
                # .pth files, including pywin32's DLL search initialization.
                bootstrap="import site,sys,runpy;site.addsitedir(sys.argv.pop(1));sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
                return subprocess.Popen([sys._base_executable, "-B", "-c", bootstrap,
                                         str(Path(sys.prefix)/'Lib/site-packages'), str(ROOT/'ocx_service.py'), 'serve'], env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            def ready(process):
                for _ in range(300):
                    if process.poll() is not None:
                        self.fail('service exited: '+process.stderr.read().decode(errors='replace')[-800:])
                    state = status(data)
                    if state['running']: return state
                    time.sleep(.1)
                self.fail('service did not become ready')
            first = launch(); second = None
            try:
                first_state = ready(first)
                self.assertTrue(first_state['official_route_ready'])
                self.assertFalse(first_state['external_route_ready'])
                internal = json.loads((data/'gateway-state.json').read_text(encoding='utf-8'))
                conn = http.client.HTTPConnection('127.0.0.1', first_state['port'], timeout=2)
                conn.request('GET','/v1/_provider_hub/health'); self.assertEqual(conn.getresponse().status,401); conn.close()
                duplicate = launch(); self.assertEqual(duplicate.wait(timeout=12),0); duplicate.stderr.close()
                first.terminate(); first.wait(timeout=5)
                for _ in range(50):
                    try:
                        with closing(http.client.HTTPConnection('127.0.0.2', internal['inner_port'], timeout=.1)) as conn:
                            conn.request('GET','/healthz'); conn.getresponse().read()
                    except (OSError, http.client.HTTPException): break
                    time.sleep(.1)
                else: self.fail('owned engine survived its supervisor')
                second = launch(); restarted = ready(second)
                self.assertEqual(restarted['port'],first_state['port'])
            finally:
                for process in (first, second):
                    if process is not None and process.poll() is None:
                        process.terminate(); process.wait(timeout=8)
                    if process is not None: process.stderr.close()
                if (data/'gateway-state.json').exists():
                    inner = json.loads((data/'gateway-state.json').read_text(encoding='utf-8'))['inner_port']
                    for _ in range(50):
                        try:
                            with closing(http.client.HTTPConnection('127.0.0.2', inner, timeout=.1)) as conn:
                                conn.request('GET','/healthz'); conn.getresponse().read()
                        except (OSError, http.client.HTTPException): break
                        time.sleep(.1)


if __name__ == '__main__': unittest.main()
