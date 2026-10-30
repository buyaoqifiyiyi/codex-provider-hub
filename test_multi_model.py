"""Multi-model contracts using synthetic stores, keyrings and loopback fixtures only."""
from copy import deepcopy
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
import time
from unittest.mock import patch

# mcp_server constructs a default store at import time; isolate that as well.
with tempfile.TemporaryDirectory(prefix='hub-mcp-import-') as import_root:
    with patch.dict(os.environ, {'LOCALAPPDATA': import_root, 'CODEX_HOME': str(Path(import_root)/'codex')}):
        import mcp_server
from storage import ProfileStore, _dpapi
from catalog import compile_catalog
from engine_control import activate_provider, disable_provider, delete_provider
from front_proxy import named_route_ready
import test_engine_control as engine_fixture
from test_neutral_python_compat import alias_for


class MultiModelTests(unittest.TestCase):
    setUp = engine_fixture.EngineControlTests.setUp
    tearDown = engine_fixture.EngineControlTests.tearDown
    write = engine_fixture.EngineControlTests.write
    keyring = engine_fixture.EngineControlTests.keyring

    def multi(self, models=None):
        models=models or ['native-alpha','fixture-beta']
        return self.store.save('Shared','responses','https://fixture.invalid/v1',models[0],
                               'synthetic-shared-key',models=models,per_model_settings={
                                   models[0]:{'context_window':64000,'reasoning_levels':['high'],'default_effort':'high'},
                                   models[1]:{'context_window':1000000,'reasoning_levels':['high','max'],'default_effort':'max'}})

    def publish(self, store=None):
        store=store or self.store
        incoming=[]
        for name,p in self.config['providers'].items():
            for model in p.get('models',[]):
                incoming.append({'slug':name+'/'+model,'context_window':32768,
                                 'supported_reasoning_levels':[{'effort':'high'}]})
        if getattr(self,'fail_publish',False): raise ValueError('fixture catalog failure')
        result=compile_catalog({'models':[]},{'models':incoming},store)
        (store.root/'catalog.json').write_text(json.dumps(result),encoding='utf-8')
        return result

    def save_ui(self, **kwargs):
        args=dict(name='Shared',protocol='responses',base_url='https://fixture.invalid/v1',
                  model='native-alpha',models=['native-alpha','fixture-beta'],key='synthetic-shared-key')
        args.update(kwargs)
        from save_jobs import SaveJobs
        jobs=SaveJobs(store_factory=lambda:ProfileStore(self.store.root),manager_factory=lambda root:self.manager,
                      keyring_fn=self.keyring,publish_fn=lambda store,manager:self.publish(store))
        with patch('mcp_server.ProfileStore',return_value=self.store),patch('mcp_server._save_jobs',return_value=jobs):
            result=mcp_server.save_provider(**args)
            if result.get('job_id'):
                end=time.monotonic()+10
                while jobs.status(result['job_id'])['state'] not in {'ready','failed','cancelled'} and time.monotonic()<end: time.sleep(.01)
                if jobs.status(result['job_id'])['state']=='ready': self.assertTrue(jobs.commit(result['job_id'])['ok'])
                job=jobs.jobs[result['job_id']];thread=job.get('thread')
                if thread: thread.join(15);self.assertFalse(thread.is_alive())
                status=jobs.status(result['job_id'])
                return status.get('result') or {'ok':False,'message':status.get('message'),'code':status.get('state')}
            return result

    def test_models_share_one_connection_and_have_independent_routes_and_catalog(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            result=activate_provider(self.store,p['id'],self.manager,self.keyring,publish=self.publish)
        self.assertTrue(result['ok'],result)
        self.assertEqual(len([p for p in self.store.list() if p['name']=='Shared']),1)
        self.assertEqual(len(result['aliases']),2)
        self.assertIn(alias_for(self.store,p['id']),result['aliases'])
        for alias in result['aliases']:
            self.assertTrue(named_route_ready(self.store,self.home,alias))
            route,secret=self.store.resolve_route(alias)
            self.assertEqual(secret,'synthetic-shared-key')
            self.assertEqual(alias.split('/',1)[1],route['model'])
        rows=json.loads((self.store.root/'catalog.json').read_text())['models']
        self.assertEqual({r['context_window'] for r in rows},{64000,1000000})
        self.assertEqual(self.store.model_settings(p['id'],'fixture-beta')['default_effort'],'max')

    def test_failure_of_second_probe_is_zero_profile_and_credential_changes(self):
        before=self.store.list()
        def probe(route,key,effort):
            return {'ok':route['model']!='fixture-beta','code':'upstream_rejected','http_status':403}
        with patch('probe.probe_snapshot',side_effect=probe) as calls,patch('probe.discover_capabilities',return_value={}):
            result=self.save_ui()
        self.assertFalse(result['ok'])
        self.assertEqual(result['failed_model'],'fixture-beta')
        self.assertEqual(calls.call_count,2)
        self.assertEqual(self.store.list(),before)
        self.assertNotIn('synthetic-shared-key',str(result))

    def test_capabilities_are_discovered_for_each_model_and_overrides_only_first(self):
        def discover(route,key):
            return {'context_window':256000 if route['model']=='native-alpha' else 1000000}
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',side_effect=discover) as calls:
            result=self.save_ui(context_window=64000)
        self.assertTrue(result['ok'],result)
        self.assertEqual(calls.call_count,2)
        ident=result['profile']['id']
        self.assertEqual(self.store.model_settings(ident,'native-alpha')['context_window'],64000)
        self.assertEqual(self.store.model_settings(ident,'fixture-beta')['context_window'],1000000)
        snapshot,_=self.store.resolve_profile(ident)
        self.assertEqual(mcp_server._consume_pretest(ident),snapshot['_revision'])
        self.assertIsNone(mcp_server._consume_pretest(ident))
        with self.store.connection() as db:
            receipts=db.execute('SELECT model,revision FROM model_probe_receipts WHERE provider_id=?',(ident,)).fetchall()
        self.assertEqual({(row[0],row[1]) for row in receipts},
                         {(model,snapshot['_revision']) for model in ('native-alpha','fixture-beta')})

    def test_second_engine_write_failure_restores_old_routes_and_discards_new_keys(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            self.assertTrue(activate_provider(self.store,p['id'],self.manager,self.keyring)['ok'])
            before=deepcopy(self.config); keys=dict(self.keys); aliases=self.store.engine_aliases()
            replace=self.manager.replace_provider
            count=0
            def fail(name,value):
                nonlocal count
                count+=1
                if count==2: return False
                return replace(name,value)
            self.manager.replace_provider=fail
            result=activate_provider(self.store,p['id'],self.manager,self.keyring)
        self.assertFalse(result['ok'])
        self.assertTrue(result['rollback_ok'])
        self.assertEqual(self.config,before)
        self.assertEqual(self.keys,keys)
        self.assertEqual(self.store.engine_aliases(),aliases)
        self.assertTrue(all(named_route_ready(self.store,self.home,a) for a in aliases))

    def test_catalog_failure_rolls_back_bindings_engine_and_catalog(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            self.assertTrue(activate_provider(self.store,p['id'],self.manager,self.keyring,publish=self.publish)['ok'])
            before=deepcopy(self.config); catalog=(self.store.root/'catalog.json').read_bytes(); keys=dict(self.keys)
            def fail():
                self.publish()
                raise ValueError('fixture catalog failure')
            result=activate_provider(self.store,p['id'],self.manager,self.keyring,publish=fail)
        self.assertFalse(result['ok'])
        self.assertEqual(self.config,before)
        self.assertEqual(self.keys,keys)
        self.assertEqual((self.store.root/'catalog.json').read_bytes(),catalog)

    def test_disable_delete_covers_all_models_and_preserves_unrelated_connection(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            result=activate_provider(self.store,p['id'],self.manager,self.keyring)
        aliases=result['aliases']; keys=dict(self.keys)
        self.assertTrue(disable_provider(self.store,p['id'],self.manager)['ok'])
        self.assertEqual(keys,self.keys)
        self.assertTrue(all(not named_route_ready(self.store,self.home,a) for a in aliases))
        self.assertTrue(all(self.store.resolve_route(a) is None for a in aliases))
        spec=importlib.util.spec_from_file_location('disabled_fixture',Path(__file__).parent/'scripts/disabled-models.py')
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        self.assertEqual(module.disabled_models(self.store.path),sorted(aliases))
        self.assertTrue(delete_provider(self.store,p['id'],True,self.manager,self.keyring)['ok'])
        self.assertEqual(self.keys,{})
        self.assertEqual(len(self.store.list()),1) # Original fixture connection.
        self.assertEqual(self.store.model_ids(p['id']),[])

    def test_active_save_shrink_keeps_id_and_removes_deselected_engine_and_binding(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            result=activate_provider(self.store,p['id'],self.manager,self.keyring)
        kept=alias_for(self.store,p['id'])
        removed=next(a for a in result['aliases'] if a!=kept)
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}), \
             patch('engine_control.EngineManager',return_value=self.manager),patch('engine_control.keyring',side_effect=lambda a,b,c,d:self.keyring(a,b,c)), \
             patch('engine_control._requires_private_network',return_value=False),patch('catalog.refresh',side_effect=lambda *a,**kw:self.publish()):
            saved=self.save_ui(provider_id=p['id'],model='fixture-beta',models=['fixture-beta'])
        self.assertTrue(saved['ok'],saved)
        self.assertEqual(saved['profile']['id'],p['id'])
        self.assertIsNone(self.store.get_engine_binding(removed))
        self.assertNotIn(removed.split('/',1)[0],self.config['providers'])
        self.assertTrue(named_route_ready(self.store,self.home,kept))

    def test_active_save_failure_rolls_back_same_connection(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            self.assertTrue(activate_provider(self.store,p['id'],self.manager,self.keyring)['ok'])
        before=self.store.list(); config=deepcopy(self.config)
        self.fail_publish=True
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}), \
             patch('engine_control.EngineManager',return_value=self.manager),patch('engine_control.keyring',side_effect=lambda a,b,c,d:self.keyring(a,b,c)), \
             patch('engine_control._requires_private_network',return_value=False),patch('catalog.refresh',side_effect=ValueError('fixture')):
            result=self.save_ui(provider_id=p['id'],name='Changed',context_window=123000)
        self.assertFalse(result['ok'])
        self.assertEqual(self.store.list(),before)
        self.assertEqual(self.config,config)

    def test_child_settings_and_set_invalidate_fingerprint_and_receipt(self):
        p=self.multi(); ident=p['id']; before=self.store.edit_fingerprint(ident)
        snapshot,_=self.store.resolve_profile(ident)
        self.store.save('Shared','responses','https://fixture.invalid/v1','native-alpha','',ident,
                        models=p['models'],per_model_settings={'fixture-beta':{'context_window':900000}})
        self.assertNotEqual(before,self.store.edit_fingerprint(ident))
        changed,_=self.store.resolve_profile(ident)
        self.assertNotEqual(snapshot['_revision'],changed['_revision'])
        with self.assertRaisesRegex(ValueError,'发生变化'):
            self.store.save('Changed','responses','https://fixture.invalid/v1','native-alpha','',ident,expected_existing=(ident,before))

    def test_concurrent_child_edit_during_group_probe_is_rejected(self):
        p=self.multi(); edited=False
        def probe(route,key,effort):
            nonlocal edited
            if not edited:
                edited=True
                self.store.save('Concurrent','responses','https://fixture.invalid/v1',p['model'],'',p['id'],
                                models=p['models'],per_model_settings={'fixture-beta':{'context_window':800000}})
            return {'ok':True}
        with patch('probe.probe_snapshot',side_effect=probe),patch('probe.discover_capabilities',return_value={}):
            result=self.save_ui(provider_id=p['id'],models=p['models']+['new-model'])
        self.assertFalse(result['ok'])
        self.assertIn('发生变化',result['message'])
        self.assertEqual(self.store.model_settings(p['id'],'fixture-beta')['context_window'],800000)

    def test_failure_after_enclosing_save_rolls_back_external_activation(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            self.assertTrue(activate_provider(self.store,p['id'],self.manager,self.keyring)['ok'])
            config=deepcopy(self.config);keys=dict(self.keys);fingerprint=self.store.edit_fingerprint(p['id'])
            with self.assertRaisesRegex(ValueError,'outer failure'):
                with self.store.atomic():
                    self.store.save('Changed','responses','https://fixture.invalid/v1',p['model'],'',p['id'],models=p['models'])
                    self.assertTrue(activate_provider(self.store,p['id'],self.manager,self.keyring,publish=self.publish)['ok'])
                    raise ValueError('outer failure')
        self.assertEqual(self.config,config)
        self.assertEqual(self.keys,keys)
        self.assertEqual(self.store.edit_fingerprint(p['id']),fingerprint)

    def test_second_delete_failure_restores_entire_multi_connection(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            activated=activate_provider(self.store,p['id'],self.manager,self.keyring)
        before=deepcopy(self.config);keys=dict(self.keys)
        remove=self.manager.delete_provider;count=0
        def fail(name):
            nonlocal count
            count+=1
            return False if count==2 else remove(name)
        self.manager.delete_provider=fail
        result=delete_provider(self.store,p['id'],True,self.manager,self.keyring)
        self.assertFalse(result['ok'])
        self.assertEqual(self.config,before)
        self.assertEqual(self.keys,keys)
        self.assertTrue(all(named_route_ready(self.store,self.home,a) for a in activated['aliases']))

    def test_second_disable_failure_restores_entire_multi_connection(self):
        p=self.multi()
        with patch('engine_control._requires_private_network',return_value=False):
            activated=activate_provider(self.store,p['id'],self.manager,self.keyring)
        before=deepcopy(self.config)
        replace=self.manager.replace_provider;count=0
        def fail(name,value):
            nonlocal count
            count+=1
            return False if count==2 else replace(name,value)
        self.manager.replace_provider=fail
        result=disable_provider(self.store,p['id'],self.manager)
        self.assertFalse(result['ok'])
        self.assertEqual(self.config,before)
        self.assertFalse(self.store.is_disabled(p['id']))

    def test_per_model_overrides_are_strict_and_independent(self):
        overrides={'native-alpha':{'context_window':123000,'reasoning_levels':['medium','high'],'default_effort':'medium'},
                   'fixture-beta':{'context_window':900000,'compact_limit':800000,'reasoning_levels':['max'],'default_effort':'max'}}
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={'context_window':1000000}):
            result=self.save_ui(per_model_overrides=overrides)
        self.assertTrue(result['ok'],result)
        ident=result['profile']['id']
        self.assertEqual(self.store.model_settings(ident,'native-alpha')['context_window'],123000)
        self.assertEqual(self.store.model_settings(ident,'fixture-beta')['reasoning_levels'],['max'])
        invalid=[{'not-selected':{}},{'native-alpha':{'unexpected':1}},
                 {'native-alpha':{'context_window':True}},
                 {'native-alpha':{'context_window':10,'compact_limit':11}},
                 {'native-alpha':{'reasoning_levels':['high'],'default_effort':'max'}}]
        for overrides in invalid:
            with self.subTest(overrides=overrides),patch('probe.probe_snapshot') as probe:
                failed=self.save_ui(per_model_overrides=overrides)
                self.assertFalse(failed['ok'])
                probe.assert_not_called()

    def test_slow_read_times_out_without_a_late_save(self):
        started=threading.Event();release=threading.Event();finished=threading.Event()
        before=self.store.list()
        def slow(*args):
            started.set();release.wait(3);finished.set();return {'ok':True}
        try:
            with patch('probe.probe_snapshot',side_effect=slow),patch('mcp_server.SAVE_READ_SECONDS',0.03):
                tick=time.monotonic();result=self.save_ui(models=['native-alpha'])
            self.assertLess(time.monotonic()-tick,1)
            self.assertTrue(started.is_set())
            self.assertFalse(result['ok'])
            self.assertEqual(result['code'],'save_deadline')
            self.assertEqual(self.store.list(),before)
            release.set();self.assertTrue(finished.wait(1))
            self.assertEqual(self.store.list(),before)
        finally: release.set()

    def test_oversized_sync_batch_is_rejected_before_probes_or_writes(self):
        models=['fixture-model'+str(n) for n in range(5001)]
        before=self.store.list()
        with patch('probe.probe_snapshot') as probe:
            failed=self.save_ui(model=models[0],models=models)
        self.assertFalse(failed['ok'])
        self.assertIn('最多 5000 项',failed['message'])
        probe.assert_not_called()
        self.assertEqual(self.store.list(),before)

    def test_jev_multiple_models_are_pretested_and_active_selection_updates_in_place(self):
        from jev_hub import JevCredentials
        models=['jev-primary','jev-backup']
        with patch('probe.probe_jev',return_value={'ok':True}) as probe:
            first=self.save_ui(protocol='system_one',base_url='https://fixture.invalid/systemone',model=models[0],models=models)
        self.assertTrue(first['ok'],first)
        self.assertEqual([c.args[0]['model'] for c in probe.call_args_list],models)
        ident=first['profile']['id'];self.store.set_jev_profile(ident)
        credentials=JevCredentials(self.store)
        original=credentials.read_settings()
        self.assertEqual(original['model'],models[0])
        self.assertEqual(self.store.model_ids(ident),models)
        # Selecting an already-saved model does not repeat the short test.
        with patch('probe.probe_jev') as probe:
            second=self.save_ui(protocol='system_one',base_url='https://fixture.invalid/systemone',provider_id=ident,
                                model=models[1],models=list(reversed(models)))
        probe.assert_not_called()
        self.assertTrue(second['ok'],second)
        self.assertEqual(second['profile']['id'],ident)
        self.assertEqual(self.store.get_jev_profile_id(),ident)
        self.assertEqual(credentials.read_settings()['model'],models[1])
        self.assertEqual(credentials.api_key(credentials.read_settings()['provider_name']),'synthetic-shared-key')
        with self.assertRaisesRegex(ValueError,'连接已更新'):
            credentials.api_key(original['provider_name'])
        self.assertEqual(self.store.model_ids(ident),list(reversed(models)))
        with patch('probe.probe_jev',return_value={'ok':False,'code':'upstream_rejected'}):
            failed=self.save_ui(protocol='system_one',base_url='https://fixture.invalid/systemone',provider_id=ident,
                                model=models[1],models=list(reversed(models))+['jev-new'],name='Failed edit')
        self.assertFalse(failed['ok'])
        self.assertEqual(failed['failed_model'],'jev-new')
        self.assertEqual(credentials.read_settings()['model'],models[1])
        self.assertEqual(self.store.model_ids(ident),list(reversed(models)))

    def test_old_storage_writer_cannot_insert_or_update_after_migration(self):
        p=self.multi();before=self.store.list();fingerprint=self.store.edit_fingerprint(p['id'])
        # A legacy SQLite writer lacks the version function required by triggers.
        with closing(sqlite3.connect(self.store.path)) as legacy:
            for sql,args in [
                ('UPDATE providers SET name=? WHERE id=?',('Old update',p['id'])),
                ('INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?)',
                 ('legacy-id','Old new','responses','https://fixture.invalid/v1','new-model',b'synthetic')),
                ('INSERT INTO model_routes(alias,provider_id,model) VALUES (?,?,?)',('legacy-alias',p['id'],p['model']))]:
                with self.assertRaises(sqlite3.Error): legacy.execute(sql,args)
                legacy.rollback()
            self.assertEqual(legacy.execute('SELECT model FROM providers WHERE id=?',(p['id'],)).fetchone()[0],p['model'])
        self.assertEqual(self.store.list(),before)
        self.assertEqual(self.store.edit_fingerprint(p['id']),fingerprint)

    def test_legacy_schema_migrates_binding_disabled_and_effort_without_reactivation(self):
        root=self.root/'legacy'; root.mkdir(); path=root/'profiles.sqlite'
        ident='12345678-1234-1234-1234-123456789abc'; key=_dpapi(b'synthetic-key')
        row=(ident,'Legacy','chat_completions','https://fixture.invalid/v1','fixture-beta',key)
        alias='legacy-route/fixture-beta'
        revision=ProfileStore._revision(row); account='fixture-route/12345678-1234-1234-1234-123456789abc'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE providers(id TEXT PRIMARY KEY,name TEXT,protocol TEXT,base_url TEXT,model TEXT,encrypted_key BLOB,updated_at INTEGER DEFAULT 1)')
            db.execute('INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?)',row)
            db.execute('CREATE TABLE model_routes(alias TEXT PRIMARY KEY,provider_id TEXT)')
            db.execute('INSERT INTO model_routes VALUES (?,?)',(alias,ident))
            db.execute('CREATE TABLE engine_bindings(alias TEXT PRIMARY KEY,provider_id TEXT,revision TEXT,account TEXT)')
            db.execute('INSERT INTO engine_bindings VALUES (?,?,?,?)',(alias,ident,revision,account))
            db.execute('CREATE TABLE disabled_connections(provider_id TEXT PRIMARY KEY)')
            db.execute('INSERT INTO disabled_connections VALUES (?)',(ident,))
            db.execute('CREATE TABLE verified_efforts(provider_id TEXT,revision TEXT,effort TEXT,PRIMARY KEY(provider_id,revision,effort))')
            db.execute('INSERT INTO verified_efforts VALUES (?,?,?)',(ident,revision,'high'))
        with self.assertRaisesRegex(ValueError,'离线迁移'):
            ProfileStore(root).list()
        store=ProfileStore(root,allow_offline_migration=True)
        self.assertEqual(store.model_ids(ident),['fixture-beta'])
        self.assertTrue(store.is_disabled(ident))
        self.assertEqual(store.get_engine_binding(alias)['account'],account)
        snapshot,_=store.resolve_profile(ident)
        self.assertEqual(store.confirmed_efforts(snapshot),{'high'})
        first=store.edit_fingerprint(ident)
        self.assertEqual(store.edit_fingerprint(ident),first)
        self.assertIsNone(store.resolve_route(alias))


if __name__=='__main__': unittest.main()
