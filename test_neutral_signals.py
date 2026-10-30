"""Public-score signals must follow the currently configured source and expiry."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import public_evaluations as refs
from storage import ProfileStore

ROOT = Path(__file__).resolve().parent


class SignalTests(unittest.TestCase):
    def test_stale_or_changed_public_reference_is_not_used_for_scheduling(self):
        with tempfile.TemporaryDirectory(prefix='neutral-signals-') as temp:
            local = Path(temp).resolve()
            if local.parent != Path(tempfile.gettempdir()).resolve():
                raise ValueError('unsafe test directory')
            store = ProfileStore(local/'CodexProviderHub')
            profile = store.save('fixture','responses','https://fixture.invalid/v1','fixture-model','synthetic-key')
            selected, _ = store.resolve_profile(profile['id'])
            alias = 'fixture-route/fixture-model'
            account = 'fixture-route/12345678-1234-1234-1234-123456789abc'
            store.bind_engine_route(alias,profile['id'],selected['_revision'],account)
            signature = refs._source_signature(refs._load_sources(store.root),'fixture-model')
            payload = {'model_id':'fixture-model','status':'available','_source_signature':signature,
                       'results':[{'benchmark':'fixture','task_id':'fixture','score':10}]}
            with refs._db(store.root) as db:
                db.execute('INSERT INTO public_references(model,payload,expires) VALUES (?,?,?)',
                           ('fixture-model',json.dumps(payload),time.time()+300))
            env = dict(os.environ,LOCALAPPDATA=str(local),CODEX_HOME=str(local/'codex'))
            def signals():
                result = subprocess.run([sys.executable,'-B',str(ROOT/'scripts/read-auto-signals.py')],
                                        env=env,capture_output=True,text=True,encoding='utf8',timeout=10)
                self.assertEqual(result.returncode,0)
                return json.loads(result.stdout)
            self.assertIn(alias,signals()['references'])
            (store.root/'public-reference-sources.json').write_text(json.dumps({
                'schemaVersion':1,'allowedReportHosts':[],
                'models':{'fixture-model':{'type':'huggingface','repository':'publisher/fixture-model'}}}),encoding='utf8')
            self.assertNotIn(alias,signals()['references'])
            with refs._db(store.root) as db:
                db.execute('UPDATE public_references SET expires=? WHERE model=?',(time.time()-1,'fixture-model'))
            self.assertNotIn(alias,signals()['references'])


if __name__ == '__main__': unittest.main()
