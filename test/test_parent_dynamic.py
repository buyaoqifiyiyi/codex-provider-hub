"""Isolated SQLite, catalog and parent-route fixtures; no credentials/network."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from storage import ProfileStore
import provider_hub_route as runtime

def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
catalog=load('dynamic_catalog','read-model-catalog.py')
binding=load('dynamic_binding','binding-kind.py')
parent=load('dynamic_parent','check-parent-route.py')
ALIAS='Vendor/Team/Model-X'
SESSION='00000000-0000-4000-8000-000000000001'
TURN='00000000-0000-4000-8000-000000000002'
BASE='http://127.0.0.1:24567/v1'

class DynamicMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='hub-dynamic-synthetic-');self.addCleanup(self.temp.cleanup)
        self.local=Path(self.temp.name);self.root=self.local/'CodexProviderHub';self.root.mkdir()
        self.home=self.local/'codex';self.home.mkdir();self.database=self.root/'profiles.sqlite'
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':str(self.local),'CODEX_HOME':str(self.home)})
        self.env.start();self.addCleanup(self.env.stop)
        self.network=patch('socket.create_connection',side_effect=AssertionError('real network forbidden'))
        self.network.start();self.addCleanup(self.network.stop)
        self.account='hub-synthetic/account-synthetic'
        with closing(sqlite3.connect(self.database)) as db:
            db.executescript('CREATE TABLE providers(id TEXT,name TEXT,protocol TEXT,base_url TEXT,model TEXT,encrypted_key BLOB);'
              'CREATE TABLE connection_models(provider_id TEXT,model TEXT,position INT,context_window INT,compact_limit INT,reasoning_levels TEXT,default_effort TEXT);'
              'CREATE TABLE engine_bindings(alias TEXT,provider_id TEXT,model TEXT,revision TEXT,account TEXT);'
              'CREATE TABLE model_routes(alias TEXT,provider_id TEXT,model TEXT);CREATE TABLE disabled_connections(provider_id TEXT);')
            self.profile=('connection','synthetic','responses','https://example.invalid/v1','Vendor/Model-X',b'synthetic-ciphertext')
            db.execute('INSERT INTO providers VALUES(?,?,?,?,?,?)',self.profile)
            db.execute('INSERT INTO connection_models VALUES(?,?,?,?,?,?,?)',('connection','Vendor/Model-X',0,12000,None,None,None))
            revision=ProfileStore._content_revision(db,self.profile)
            db.execute('INSERT INTO engine_bindings VALUES(?,?,?,?,?)',(ALIAS,'connection','Vendor/Model-X',revision,self.account))
            db.execute('INSERT INTO model_routes VALUES(?,?,?)',(ALIAS,'connection','Vendor/Model-X'))
            db.execute('INSERT INTO providers VALUES(?,?,?,?,?,?)',('subscription','official','codex_subscription','https://chatgpt.com/backend-api/codex','native-fixture',None))
            db.execute('INSERT INTO connection_models VALUES(?,?,?,?,?,?,?)',('subscription','native-fixture',0,None,None,None,None))
            db.commit()
        (self.home/'models_cache.json').write_text(json.dumps({'models':[{'slug':'native-fixture'}]}),encoding='utf-8')
        self.catalog_file=self.home/'catalog.json'
        (self.home/'config.toml').write_text('model_catalog_json='+json.dumps(str(self.catalog_file)),encoding='utf-8')
        self.catalog_file.write_text(json.dumps({'models':[{'slug':s,'context_window':12000,'supported_reasoning_levels':[{'effort':'minimal'},{'effort':'high'}]} for s in [ALIAS,'native-fixture','unmanaged-model']]}),encoding='utf-8')
        self.engine=self.root/'engine-state';self.engine.mkdir()
        self.config={'providers':{'hub-synthetic':{'adapter':'openai-responses','baseUrl':'https://example.invalid/v1','apiKey':'keychain:'+self.account,'defaultModel':'Vendor/Model-X','models':['Vendor/Model-X']}}}
        self.write_engine()
    def write_engine(self): (self.engine/'config.json').write_text(json.dumps(self.config),encoding='utf-8')
    def mutate(self,sql,args=()):
        with closing(sqlite3.connect(self.database)) as db: db.execute(sql,args);db.commit()
    def result(self): return catalog.read_catalog(self.home)
    def test_catalog_contract_is_sanitized_and_intersected(self):
        data=self.result();self.assertEqual(set(data['hubModels']),{ALIAS});self.assertEqual(data['managedOfficialModels'],['native-fixture'])
        self.assertEqual(set(data['hubModels'][ALIAS]),{'revision','model','connection','display_name'})
        self.assertEqual(len(data['hubModels'][ALIAS]['revision']),64)
        self.assertEqual(data['modelDetails'][ALIAS],{'context_window':12000,'source':'catalog_configuration','verified':False})
        text=json.dumps(data);self.assertNotIn(self.account,text);self.assertNotIn('synthetic-ciphertext',text);self.assertNotIn('example.invalid',text)
    def test_revision_mismatch_excludes_binding(self):
        self.mutate('UPDATE connection_models SET context_window=24000 WHERE provider_id=?',('connection',))
        self.assertEqual(self.result()['hubModels'],{})
        self.assertEqual(binding.binding_kind(self.database,ALIAS)['kind'],'unknown')
        self.assertEqual(binding.binding_kind(self.database,'absent')['kind'],'unbound')
    def test_disabled_excludes_both_authorization_paths(self):
        for connection in ['connection','subscription']:self.mutate('INSERT INTO disabled_connections VALUES(?)',(connection,))
        self.assertEqual(self.result()['hubModels'],{});self.assertEqual(self.result()['managedOfficialModels'],[])
    def test_missing_child_route_or_binding_never_authorizes(self):
        for table in ['connection_models','model_routes','engine_bindings']:
            with self.subTest(table=table):
                with closing(sqlite3.connect(self.database)) as db:
                    db.execute('BEGIN');db.execute('DELETE FROM '+table)
                    self.assertEqual(binding.active_bindings(db),{});db.rollback()
    def test_catalog_removal_and_visibility_exclude_managed(self):
        self.catalog_file.write_text(json.dumps({'models':[{'slug':ALIAS,'visibility':'hide','supported_reasoning_levels':[{'effort':'high'}]}]}),encoding='utf-8')
        data=self.result();self.assertEqual(data['hubModels'],{});self.assertEqual(data['managedOfficialModels'],[])
    def test_bound_engine_uses_target_revision(self):
        b=self.result()['hubModels'][ALIAS]
        self.assertEqual(runtime.bound_engine_route(ALIAS,b['revision']),b)
        self.assertIsNone(runtime.bound_engine_route(ALIAS,'b'*64))
        self.config['providers']['hub-synthetic']['defaultModel']='wrong';self.write_engine()
        self.assertIsNone(runtime.bound_engine_route(ALIAS,b['revision']))
    def test_engine_disabled_or_binding_changed_is_rejected(self):
        revision=self.result()['hubModels'][ALIAS]['revision']
        self.config['providers']['hub-synthetic']['disabled']=True;self.write_engine()
        self.assertIsNone(runtime.bound_engine_route(ALIAS,revision))
        self.config['providers']['hub-synthetic'].pop('disabled');self.write_engine()
        self.mutate('UPDATE engine_bindings SET account=?',('other-account',))
        self.assertIsNone(runtime.bound_engine_route(ALIAS,revision))
    def test_health_identity_is_accepted_under_generic_contract(self):
        payload={'service':'codex-provider-hub','version':1,'external_route_ready':False,'official_route_ready':False}
        with patch.object(runtime,'health_request',side_effect=[(401,None),(200,payload)]):
            self.assertTrue(runtime.authenticated_hub(BASE,'a'*40))
    def test_generic_active_source_requires_hub_and_current_engine(self):
        b=self.result()['hubModels'][ALIAS]
        route={'base_url':BASE,'http_headers':{'X-Provider-Hub-Token':'a'*40}}
        with patch.object(runtime,'custom_route',return_value=route),patch.object(runtime,'authenticated_hub',return_value=True) as health:
            self.assertEqual(runtime.active_source(ALIAS,b['revision'])['binding'],b)
            health.assert_called_with(BASE,'a'*40)
            self.mutate('INSERT INTO disabled_connections VALUES(?)',('connection',))
            self.assertIsNone(runtime.active_source(ALIAS,b['revision']))
    def test_same_turn_route_uses_exact_alias_and_rechecks_source(self):
        b=self.result()['hubModels'][ALIAS]
        (self.home/'config.toml').write_text('model_catalog_json='+json.dumps(str(self.catalog_file))+'\nmodel_provider="custom"\n[model_providers.custom]\nbase_url="'+BASE+'"\n',encoding='utf-8')
        with closing(sqlite3.connect(self.home/'logs_2.sqlite')) as db:
            db.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY,thread_id TEXT,target TEXT,feedback_log_body TEXT)')
            db.execute('INSERT INTO logs(thread_id,target,feedback_log_body) VALUES(?,?,?)',(SESSION,'codex_http_client::client',f'turn.id="{TURN}": Request completed method=POST url={BASE}/responses status=200'));db.commit()
        source={'kind':'hub','base_url':BASE,'binding':b}
        with patch.object(parent,'active_source',return_value=source) as active:
            self.assertEqual(parent.main(SESSION,TURN,ALIAS,b['revision']),'compatible');self.assertEqual(active.call_count,2)
            active.assert_called_with(ALIAS,b['revision'])
        with patch.object(parent,'active_source',side_effect=[source,None]):
            self.assertEqual(parent.main(SESSION,TURN,ALIAS,b['revision']),'unknown')
    def test_database_remains_read_only(self):
        before=self.database.read_bytes();self.result();binding.binding_kind(self.database,ALIAS)
        self.assertEqual(self.database.read_bytes(),before)

if __name__=='__main__':unittest.main()
