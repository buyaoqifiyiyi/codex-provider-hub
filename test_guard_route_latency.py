"""Verify delayed same-turn evidence without accepting stale or failed calls."""
import importlib.util
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parent / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('check_parent_route', SCRIPTS / 'check-parent-route.py')
route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(route)
SESSION = '20000000-0000-4000-8000-000000000001'
TURN = '20000000-0000-4000-8000-000000000002'
OTHER = '20000000-0000-4000-8000-000000000003'
BASE = 'http://127.0.0.1:24567/v1'
SOURCE = {'kind':'hub','base_url':BASE,'capability':'synthetic'}


class ParentRouteLatencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hub-route-latency-')
        self.home = Path(self.temporary.name)
        (self.home/'config.toml').write_text('model_provider="custom"\n[model_providers.custom]\nbase_url="'+BASE+'"\n',encoding='utf-8')
        self.db = self.home/'logs_2.sqlite'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('create table logs(id integer primary key,thread_id text,target text,feedback_log_body text)')
            conn.commit()
        self.environ = patch.dict(os.environ, {'CODEX_HOME':str(self.home)})
        self.environ.start()
        self.active = patch.object(route, 'active_source', return_value=SOURCE)
        self.active.start()
        self.deadline = patch.object(route, 'SAME_TURN_LOG_WAIT_SECONDS', .3)
        self.deadline.start()

    def tearDown(self):
        self.deadline.stop(); self.active.stop(); self.environ.stop(); self.temporary.cleanup()

    def add(self, turn=TURN, status=200):
        body=f'turn.id="{turn}": Request completed method=POST url={BASE}/responses status={status}'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('insert into logs(thread_id,target,feedback_log_body) values(?,?,?)',
                         (SESSION, 'codex_http_client::client', body))
            conn.commit()

    def test_waits_for_a_delayed_successful_same_turn_request(self):
        def delayed():
            time.sleep(.2)
            self.add()
        worker=threading.Thread(target=delayed);worker.start()
        try:self.assertEqual(route.main(SESSION,TURN), 'compatible')
        finally:worker.join()

    def test_stale_or_failed_request_never_counts(self):
        self.add(OTHER)
        self.add(TURN, 503)
        self.assertEqual(route.main(SESSION,TURN), 'unknown')

    def test_stale_only_never_counts_as_same_turn_evidence(self):
        self.add(OTHER)
        self.assertEqual(route.main(SESSION,TURN), 'unknown')


if __name__ == '__main__': unittest.main()
