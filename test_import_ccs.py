import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from import_ccs import import_existing
from storage import ProfileStore


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI is required')
class ImportTests(unittest.TestCase):
    def test_existing_connections_import_once_without_plaintext_keys(self):
        with tempfile.TemporaryDirectory(prefix='hub-import-test-') as temp:
            root = Path(temp); source = root/'legacy.sqlite'; jev = root/'jev.json'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE providers(id TEXT,name TEXT,settings_config TEXT,meta TEXT,is_current INTEGER,app_type TEXT)')
                for ident,name,host,model,fmt,key,current in [
                    ('a','route-a','https://example.invalid/v1','example-model','openai_chat','synthetic-key-a',1),
                    ('b','route-b','https://decision.invalid','decision-model','openai_responses','synthetic-key-b',0)]:
                    config = f'model_provider="p"\nmodel="{model}"\n[model_providers.p]\nbase_url="{host}"\n'
                    data = {'auth':{'OPENAI_API_KEY':key},'config':config,'modelCatalog':{'models':[{'model':model}]}}
                    db.execute('INSERT INTO providers VALUES (?,?,?,?,?,?)',
                               (ident,name,json.dumps(data),json.dumps({'apiFormat':fmt}),current,'codex'))
                db.commit()
            jev.write_text(json.dumps({'provider_name':'route-b','endpoint':'https://decision.invalid/v1/systemone','model':'decision-model'}),encoding='utf-8')
            store=ProfileStore(root/'new')
            result=import_existing(store,source,jev)
            self.assertEqual(result['imported'],2); self.assertTrue(result['current_profile']); self.assertTrue(result['jev_profile'])
            self.assertEqual(import_existing(store,source,jev)['imported'],0)
            self.assertEqual(len(store.list()),2)
            # A previously imported and intentionally deleted profile must not revive.
            with closing(store._connect()) as db, db:
                db.execute('DELETE FROM providers WHERE id=?', (result['current_profile'],))
            again = import_existing(store,source,jev)
            self.assertEqual(again['imported'], 0)
            self.assertIsNone(again['current_profile'])
            self.assertEqual(len(store.list()), 1)
            self.assertNotIn(b'synthetic-key-a',store.path.read_bytes())
            self.assertNotIn(b'synthetic-key-b',store.path.read_bytes())


if __name__ == '__main__': unittest.main()
