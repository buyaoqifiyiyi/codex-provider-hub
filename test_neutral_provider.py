"""Provider-neutral routing and upgrade checks using isolated synthetic state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from catalog import compile_catalog
from config_migration import apply_config, plan_config
from engine_control import activate_provider, public_aliases
from front_proxy import read_engine_routes
from storage import OFFICIAL_CODEX_URL, ProfileStore, effective_model_settings, merge_discovered_model_settings


class ProviderNeutralTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='neutral-provider-')
        self.root = Path(self.temp.name).resolve()
        if self.root.parent != Path(tempfile.gettempdir()).resolve():
            raise ValueError('unsafe test directory')
        self.home = self.root / 'engine'
        self.home.mkdir()
        self.store = ProfileStore(self.root / 'store')
        self.keys = {}
        self.config = {'defaultProvider':'openai','providers':{'openai':{
            'adapter':'openai-responses','baseUrl':OFFICIAL_CODEX_URL,
            'authMode':'forward','codexAccountMode':'direct'}}}
        self.write()
        outer = self
        class Manager:
            home = outer.home
            def snapshot(self): return deepcopy(outer.config)
            def replace_provider(self, name, value):
                outer.config['providers'][name] = deepcopy(value); outer.write(); return True
            def delete_provider(self, name):
                outer.config['providers'].pop(name, None); outer.write(); return True
        self.manager = Manager()

    def tearDown(self): self.temp.cleanup()
    def write(self): (self.home/'config.json').write_text(json.dumps(self.config),encoding='utf8')
    def keyring(self, action, account, secret):
        if action == 'write':
            if account in self.keys: return False
            self.keys[account] = secret; return True
        if action == 'discard': return self.keys.pop(account, None) is not None
        return False

    def profile(self, name, model, settings=None):
        return self.store.save(name, 'chat_completions', 'https://example.invalid/v1',
                               model, 'synthetic-key', settings=settings)

    def activate(self, profile):
        with patch('engine_control._requires_private_network',return_value=False):
            result=activate_provider(self.store,profile['id'],self.manager,self.keyring)
        self.assertTrue(result['ok'],result)
        return result

    def test_same_model_across_connections_gets_distinct_bindings(self):
        # Regression model is an ordinary ID, not a source of routing policy.
        model='shared-fixture-model'
        a=self.profile('first',model);b=self.profile('second',model)
        x=self.activate(a);y=self.activate(b)
        self.assertNotEqual(x['alias'],y['alias'])
        self.assertEqual(self.store.get_engine_binding(x['alias'])['id'],a['id'])
        self.assertEqual(self.store.get_engine_binding(y['alias'])['id'],b['id'])

    def test_existing_alias_is_preserved_from_data(self):
        p=self.profile('legacy','fixture-model')
        selected,_=self.store.resolve_profile(p['id'])
        alias='legacy-namespace/fixture-model'
        account='legacy-namespace/'+str(uuid.uuid4())
        self.store.bind_engine_route(alias,p['id'],selected['_revision'],account,model='fixture-model')
        self.assertEqual(public_aliases(self.store,p['id'],['fixture-model'])['fixture-model'],alias)

    def test_unknown_capabilities_are_not_invented(self):
        blank={'context_window':None,'compact_limit':None,'reasoning_levels':None,'default_effort':None}
        self.assertEqual(effective_model_settings(blank),blank)
        p=self.profile('example','model-a')
        r=self.activate(p)
        binding=self.store.get_engine_binding(r['alias'])
        provider=self.config['providers'][binding['account'].split('/',1)[0]]
        for name in ('modelContextWindows','modelAutoCompactTokenLimits',
                     'modelReasoningEfforts','modelDefaultReasoningEfforts'):
            self.assertNotIn(name,provider)
        engine={'models':[{'slug':r['alias'],'context_window':48000,
                           'supported_reasoning_levels':[{'effort':'medium'}],
                           'default_reasoning_level':'medium'}]}
        row=compile_catalog({'models':[]},engine,self.store)['models'][0]
        self.assertEqual(row['context_window'],48000)
        self.assertEqual(row['default_reasoning_level'],'medium')
        self.assertNotIn('auto_compact_token_limit',row)

    def test_discovered_capabilities_fill_only_missing_values(self):
        blank={'context_window':None,'compact_limit':None,'reasoning_levels':None,'default_effort':None}
        detected={'context_window':48000,'reasoning_levels':['low','medium'],'default_effort':'medium'}
        result=merge_discovered_model_settings('model-a',blank,detected)
        self.assertEqual(result,{'context_window':48000,'compact_limit':None,
                                 'reasoning_levels':['low','medium'],'default_effort':'medium'})
        manual=dict(blank,context_window=64000,reasoning_levels=['high'],default_effort='high')
        self.assertEqual(merge_discovered_model_settings('model-a',manual,detected),manual)
        reference=dict(detected,reference_sources={'context_window':'vendor-reference','reasoning_levels':'vendor-reference'})
        self.assertEqual(merge_discovered_model_settings('model-a',blank,reference),blank)

    def test_explicit_capabilities_are_preserved(self):
        settings={'context_window':65536,'compact_limit':50000,
                  'reasoning_levels':['low','medium'],'default_effort':'low'}
        p=self.profile('configured','model-b',settings)
        r=self.activate(p);binding=self.store.get_engine_binding(r['alias'])
        provider=self.config['providers'][binding['account'].split('/',1)[0]]
        self.assertEqual(provider['modelContextWindows']['model-b'],65536)
        self.assertEqual(provider['modelAutoCompactTokenLimits']['model-b'],50000)
        self.assertEqual(provider['modelReasoningEfforts']['model-b'],['low','medium'])
        self.assertEqual(provider['modelDefaultReasoningEfforts']['model-b'],'low')

    def test_route_readiness_is_independent_of_other_connections(self):
        p=self.profile('example','model-c');r=self.activate(p)
        self.assertEqual(read_engine_routes(self.store,self.home),(True,True))
        self.config['providers'].pop('openai')
        self.config['defaultProvider']='unused'
        self.write()
        self.assertEqual(read_engine_routes(self.store,self.home),(False,True))

    def migrate(self, model, state):
        home=self.root/'codex';home.mkdir(exist_ok=True)
        self.store.root.mkdir(exist_ok=True)
        config=home/'config.toml'
        config.write_text('model_provider="custom"\nmodel='+json.dumps(model)+'\n'
                          '[model_providers.custom]\nbase_url="http://127.0.0.1:24568/v1"\n',encoding='utf8')
        (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':'native-fixture'}]}),encoding='utf8')
        (self.store.root/'catalog.json').write_text(json.dumps({'models':[{'slug':model}]}),encoding='utf8')
        with patch('config_migration.ocx_service.status',return_value=state), \
             patch('config_migration.guard_verified',return_value=True), \
             patch('config_migration.startup_verified',return_value=True), \
             patch('config_migration._read_capability',return_value='synthetic-capability'):
            return apply_config(self.store.root,home)

    def test_enable_with_only_native_route(self):
        state={'running':True,'port':23456,'official_route_ready':True,'external_route_ready':False}
        self.assertTrue(self.migrate('native-fixture',state)['changed'])

    def test_enable_with_only_selected_bound_route(self):
        p=self.profile('example','model-c');r=self.activate(p)
        engine=self.store.root/'engine-state';engine.mkdir()
        (engine/'config.json').write_text(json.dumps(self.config),encoding='utf8')
        state={'running':True,'port':23456,'official_route_ready':False,'external_route_ready':True}
        self.assertTrue(self.migrate(r['alias'],state)['changed'])

    def test_unbound_catalog_entry_is_not_treated_as_native(self):
        self.store.root.mkdir(exist_ok=True)
        (self.store.root/'catalog.json').write_text(json.dumps({'models':[{'slug':'unknown-model'}]}),encoding='utf8')
        source=('model_provider="custom"\nmodel="unknown-model"\n'
                '[model_providers.custom]\nbase_url="http://127.0.0.1:24568/v1"\n')
        with patch('config_migration.native_model_ids',return_value=set()):
            with self.assertRaises(ValueError):
                plan_config(source,self.store.root,23456,'synthetic-capability',False,self.root)

    def test_native_and_bound_identity_conflict_is_rejected(self):
        p=self.profile('example','model-c');selected,_=self.store.resolve_profile(p['id'])
        alias='native-conflict';account='fixture-'+uuid.uuid4().hex+'/'+str(uuid.uuid4())
        self.store.bind_engine_route(alias,p['id'],selected['_revision'],account,model='model-c')
        home=self.root/'native-home';home.mkdir()
        (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':alias}]}),encoding='utf8')
        with patch.dict(os.environ,{'CODEX_HOME':str(home)}):
            with self.assertRaisesRegex(ValueError,'冲突'):
                compile_catalog({'models':[{'slug':alias}]},{'models':[{'slug':alias}]},self.store)


if __name__ == '__main__': unittest.main()
