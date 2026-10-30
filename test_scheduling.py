import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from storage import ProfileStore

ROOT = Path(__file__).resolve().parent
ROUTE = 'fixture-route/fixture-chat'
DEFAULTS={'bounded':{'model':ROUTE,'effort':'high','fallback_model':'native-alpha','fallback_effort':'medium'},
          'boundary':{'model':ROUTE,'effort':'max','fallback_model':'native-beta','fallback_effort':'high'},
          'critical':{'model':'native-beta','effort':'high','fallback_model':'native-gamma','fallback_effort':'high'}}


def binding_module():
    spec=importlib.util.spec_from_file_location('binding_kind_fixture',ROOT/'scripts'/'binding-kind.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


class DynamicSchedulingTests(unittest.TestCase):
    def test_disable_and_switch_are_visible_without_upstream_requests(self):
        with tempfile.TemporaryDirectory(prefix='hub-routing-snapshot-') as directory:
            local=Path(directory); root=local/'CodexProviderHub'; home=local/'codex'; home.mkdir()
            (home/'catalog.json').write_text(json.dumps({'models':[
                {'slug':ROUTE,'supported_reasoning_levels':[{'effort':'high'},{'effort':'max'}]},
                {'slug':'native-alpha','supported_reasoning_levels':[{'effort':'medium'}]},
                {'slug':'native-beta','supported_reasoning_levels':[{'effort':'high'}]},
                {'slug':'native-gamma','supported_reasoning_levels':[{'effort':'high'}]}
            ]}),encoding='utf-8')
            (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':name,'supported_reasoning_levels':[{'effort':'high'}]} for name in ('native-alpha','native-beta','native-gamma')]}),encoding='utf8')
            (home/'config.toml').write_text('model_catalog_json = '+json.dumps((home/'catalog.json').as_posix())+'\n',encoding='utf8')
            store=ProfileStore(root)
            root.mkdir(parents=True,exist_ok=True)
            (root/'scheduling-mode.json').write_text('{"mode":"manual"}',encoding='utf8')
            (root/'scheduling.json').write_text(json.dumps(DEFAULTS),encoding='utf8')
            with store.atomic() as db:
                for ident,url in [('first','https://first.invalid/v1'),('second','https://example.test/v1')]:
                    db.execute('INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?)',
                               (ident,ident,'chat_completions',url,'fixture-chat',b'synthetic-ciphertext'))
                    db.execute('INSERT INTO connection_models(provider_id,model,position) VALUES (?,?,0)',(ident,'fixture-chat'))
                for ident in ('first','second'):
                    row=db.execute('SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?',(ident,)).fetchone()
                    revision=ProfileStore._content_revision(db,row)
                    if ident=='first':
                        db.execute('INSERT INTO model_routes(alias,provider_id,model) VALUES (?,?,?)',(ROUTE,ident,'fixture-chat'))
                        db.execute('INSERT INTO engine_bindings(alias,provider_id,revision,account,model) VALUES (?,?,?,?,?)',
                                   (ROUTE,ident,revision,'fixture-route/33333333-3333-4333-8333-333333333333','fixture-chat'))
                    else: second_revision=revision
            env=dict(os.environ,LOCALAPPDATA=str(local),CODEX_HOME=str(home),PLUGIN_DATA=str(local/'audit'))
            def overview(bound,disabled=False,other_profiles=()):
                data={'providers':[{'id':bound,'name':bound,'models':['fixture-chat'],
                                    'disabled':disabled}, *other_profiles]}
                result=subprocess.run(['node',str(ROOT/'scripts'/'scheduling.cjs'),'overview'],input=json.dumps(data),
                                      capture_output=True,text=True,encoding='utf-8',check=True,env=env,timeout=8,cwd=local)
                return json.loads(result.stdout)
            self.assertEqual(binding_module().binding_kind(store.path, ROUTE)['kind'],'bound')
            first=overview('first')
            self.assertEqual(first['effective']['bounded']['model'],ROUTE,first)
            self.assertEqual(first['effective']['bounded']['connection'],'first')
            classifier=binding_module()
            self.assertEqual(classifier.binding_kind(store.path, ROUTE)['kind'],'bound')
            with store.atomic() as db:
                db.execute('INSERT INTO disabled_connections(provider_id) VALUES (?)',('first',))
            disabled=overview('first',True)
            self.assertEqual(disabled['effective']['bounded']['model'],'native-alpha')
            self.assertEqual(disabled['effective']['bounded']['source'],'fallback')
            self.assertEqual(classifier.binding_kind(store.path, ROUTE)['kind'],'disabled')
            with store.atomic() as db:
                db.execute('UPDATE model_routes SET provider_id=? WHERE alias=?',('second',ROUTE))
                db.execute('UPDATE engine_bindings SET provider_id=?,revision=? WHERE alias=?',('second',second_revision,ROUTE))
            second=overview('second')
            self.assertEqual(second['effective']['bounded']['model'],ROUTE)
            self.assertEqual(second['effective']['bounded']['connection'],'second')
            self.assertEqual(classifier.binding_kind(store.path, ROUTE)['kind'],'bound')
            mixed=overview('first',True,[{'name':'second','models':['fixture-chat'],
                                          'disabled':False}])
            saved=next(c for c in mixed['candidates'] if c['model']=='fixture-chat')
            self.assertNotEqual(saved['state'],'disabled')
            self.assertIn('first（已停用）',saved['reason']); self.assertIn('second（已保存）',saved['reason'])
            with store.atomic() as db:
                db.execute('UPDATE providers SET base_url=? WHERE id=?',('https://changed.test/v1','second'))
            self.assertEqual(classifier.binding_kind(store.path, ROUTE)['kind'],'unknown')

    def test_native_identity_and_optional_local_policy_are_independent(self):
        with tempfile.TemporaryDirectory(prefix='hub-policy-snapshot-') as directory:
            local=Path(directory);home=local/'codex';home.mkdir()
            store=ProfileStore(local/'CodexProviderHub');store.list()
            (home/'models_cache.json').write_text(json.dumps({'models':[
                {'slug':'native-alpha','supported_reasoning_levels':[{'effort':'medium'}]},
                {'slug':'native-beta','supported_reasoning_levels':[{'effort':'ultra'}]},
                {'slug':'unknown-effort','supported_reasoning_levels':[]}]}),encoding='utf8')
            env=dict(os.environ,LOCALAPPDATA=str(local),CODEX_HOME=str(home),PLUGIN_DATA=str(local/'audit'))
            code="const c=require(process.argv[1]).loadAvailableModels();const p=require(process.argv[2]);process.stdout.write(JSON.stringify({nativeModels:c.nativeModels,eligible:p.eligibleModels(p.loadPolicy(),c)}));"
            def snapshot():
                run=subprocess.run(['node','-e',code,str(ROOT/'lib/model-catalog.cjs'),str(ROOT/'lib/model-policy.cjs')],
                                   env=env,cwd=local,capture_output=True,text=True,encoding='utf8',check=True,timeout=8)
                return json.loads(run.stdout)
            absent=snapshot()
            self.assertEqual(set(absent['nativeModels']),{'native-alpha','native-beta','unknown-effort'})
            self.assertEqual(absent['eligible'],{'native-alpha':['medium'],'native-beta':['ultra']})
            policy=store.root/'model-policy.json'
            policy.write_text(json.dumps({'schemaVersion':2,'allowedFamilies':['native-beta'],
                'allowedEffortsByFamily':{},'jevAdvisory':{'enabled':False}}),encoding='utf8')
            self.assertEqual(snapshot()['eligible'],{'native-beta':['ultra']})
            policy.write_text(json.dumps({'schemaVersion':2,'allowedFamilies':[],
                'allowedEffortsByFamily':{'native-beta':['high']},'jevAdvisory':{'enabled':False}}),encoding='utf8')
            self.assertEqual(snapshot()['eligible'],{'native-alpha':['medium']})
            policy.write_text('{invalid-json',encoding='utf8')
            rejected=subprocess.run(['node','-e',code,str(ROOT/'lib/model-catalog.cjs'),str(ROOT/'lib/model-policy.cjs')],
                                    env=env,cwd=local,capture_output=True,text=True,encoding='utf8',timeout=8)
            self.assertNotEqual(rejected.returncode,0)
            self.assertIn('MODEL_POLICY_INVALID',rejected.stderr)


if __name__ == '__main__': unittest.main()
