"""Check credential handoff and rollback using synthetic local profiles."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from engine_control import _requires_private_network, activate_provider, activate_local_provider, delete_provider, disable_provider, retry_credential_cleanup
from front_proxy import read_engine_routes
from storage import ProfileStore
from test_neutral_python_compat import isolated_native_catalog, alias_for


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI is required')
class EngineControlTests(unittest.TestCase):
    def test_fake_dns_is_scoped_to_https_and_rejects_other_private_ranges(self):
        def resolved(*addresses):
            return [(2,1,6,'',(address,443)) for address in addresses]
        with patch('engine_control.socket.getaddrinfo',return_value=resolved('198.18.0.1')):
            self.assertTrue(_requires_private_network('https://example.invalid/v1'))
            self.assertFalse(_requires_private_network('http://example.invalid/v1'))
        with patch('engine_control.socket.getaddrinfo',return_value=resolved('198.18.0.1','10.0.0.5')):
            self.assertFalse(_requires_private_network('https://example.invalid/v1'))
        with patch('engine_control.socket.getaddrinfo',return_value=resolved('198.18.0.1','2001:2::1')):
            self.assertTrue(_requires_private_network('https://example.invalid/v1'))
        with patch('engine_control.socket.getaddrinfo',return_value=resolved('198.18.0.1','fd00::1')):
            self.assertFalse(_requires_private_network('https://example.invalid/v1'))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hub-engine-control-')
        self.root = Path(self.tmp.name); self.home = self.root/'engine'; self.home.mkdir()
        isolated_native_catalog(self, self.root)
        self.store = ProfileStore(self.root/'store')
        self.provider = self.store.save('External', 'chat_completions', 'https://example.invalid/v1',
                                        'fixture-beta', 'synthetic-provider-key')
        self.alias = alias_for(self.store, self.provider["id"])
        self.keys = {}
        self.config = {'defaultProvider':'openai', 'providers': {'openai': {
            'adapter':'openai-responses','baseUrl':'https://chatgpt.com/backend-api/codex',
            'authMode':'forward','codexAccountMode':'direct'}}}
        self.write()
        outer = self
        class Manager:
            home = outer.home
            def snapshot(self): return deepcopy(outer.config)
            def replace_provider(self, name, profile):
                outer.config['providers'][name] = deepcopy(profile); outer.write(); return True
            def delete_provider(self, name):
                del outer.config['providers'][name]; outer.write(); return True
        self.manager = Manager()

    def write(self): (self.home/'config.json').write_text(json.dumps(self.config), encoding='utf-8')
    def tearDown(self): self.tmp.cleanup()
    def keyring(self, action, account, value):
        if action == 'write':
            if account in self.keys: return False
            self.keys[account] = value; return True
        if action == 'discard': return self.keys.pop(account, None) is not None
        return False

    def route_name(self):
        binding=self.store.get_engine_binding(self.alias)
        if binding: return binding['account'].split('/',1)[0]
        return next(name for name,value in self.config['providers'].items() if value.get('defaultModel')=='fixture-beta')

    def test_activation_uses_only_a_keychain_reference_in_engine_config(self):
        result = activate_provider(self.store, self.provider['id'], self.manager, self.keyring)
        self.assertTrue(result['ok'])
        self.assertTrue(read_engine_routes(self.store, self.home)[1])
        self.assertNotIn('synthetic-provider-key', (self.home/'config.json').read_text(encoding='utf-8'))
        reference = self.config['providers'][self.route_name()]['apiKey']
        self.assertEqual(reference,'keychain:'+self.store.get_engine_binding(self.alias)['account'])
        self.assertEqual(self.keys[reference.removeprefix('keychain:')], 'synthetic-provider-key')

    def test_unknown_window_and_efforts_are_not_invented_or_saved(self):
        result = activate_provider(self.store, self.provider['id'], self.manager, self.keyring)
        self.assertTrue(result['ok'], result)
        model = 'fixture-beta'
        entry = self.config['providers'][self.route_name()]
        for field in ('modelContextWindows', 'modelAutoCompactTokenLimits',
                      'modelReasoningEfforts', 'modelDefaultReasoningEfforts'):
            self.assertNotIn(field, entry)
        self.assertIsNone(self.store.model_settings(self.provider['id'], model)['context_window'])

    def test_changed_profile_during_update_restores_prior_route(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        before = deepcopy(self.config['providers'][self.route_name()])
        original_replace = self.manager.replace_provider
        changed = False
        def race(name, profile):
            nonlocal changed
            accepted = original_replace(name, profile)
            if not changed and profile.get('apiKey') != before.get('apiKey'):
                changed = True
                self.store.save('External','chat_completions','https://new.invalid/v1','fixture-beta',
                                'new-synthetic-key',self.provider['id'])
            return accepted
        self.manager.replace_provider = race
        result = activate_provider(self.store, self.provider['id'], self.manager, self.keyring)
        self.assertFalse(result['ok'])
        self.assertEqual(self.config['providers'][self.route_name()], before)
        self.assertEqual(len(self.keys), 1)
        self.assertFalse(read_engine_routes(self.store, self.home)[1])

    def test_other_codex_models_get_a_separate_named_route(self):
        other = self.store.save('Other', 'responses', 'https://other.invalid/v1', 'my-model', 'synthetic-other-key')
        result = activate_provider(self.store, other['id'], self.manager, self.keyring)
        self.assertTrue(result['ok'])
        self.assertTrue(result['alias'].startswith('hub-'))
        self.assertTrue(result['alias'].endswith('/my-model'))
        self.assertIsNotNone(self.store.get_engine_binding(result['alias']))

    def test_configured_window_and_efforts_reach_engine(self):
        settings = {'context_window': 128000, 'compact_limit': 100000,
                    'reasoning_levels': ['high', 'max'], 'default_effort': 'high'}
        self.store.save('External', 'chat_completions', 'https://example.invalid/v1', 'fixture-beta',
                        '', self.provider['id'], settings=settings)
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        p = self.config['providers'][self.route_name()]
        self.assertEqual(p['modelContextWindows']['fixture-beta'], 128000)
        self.assertEqual(p['modelAutoCompactTokenLimits']['fixture-beta'], 100000)
        self.assertEqual(p['modelReasoningEfforts']['fixture-beta'], ['high', 'max'])
        self.assertEqual(p['modelDefaultReasoningEfforts']['fixture-beta'], 'high')

    def test_only_matching_recent_pretest_can_skip_second_probe(self):
        p=self.store.save('Other','responses','https://example.invalid/v1','model-a','synthetic-key')
        snapshot,_=self.store.resolve_profile(p['id'])
        with patch('engine_control.default_data_root',return_value=self.root/'store'), \
             patch('engine_control.ocx_service.start',return_value={'running':True}), \
             patch('engine_control.EngineManager',return_value=self.manager), \
             patch('engine_control.activate_provider',return_value={'ok':True}), \
             patch('catalog.refresh'), patch('probe.probe_snapshot',return_value={'ok':False}) as probe:
            self.assertTrue(activate_local_provider(p['id'],pretested_revision=snapshot['_revision'])['ok'])
            probe.assert_not_called()
            self.assertFalse(activate_local_provider(p['id'],pretested_revision='stale')['ok'])
            probe.assert_called_once()

    def test_activation_preserves_safe_probe_diagnostic(self):
        p=self.store.save('Other','responses','https://example.invalid/v1','model-a','synthetic-key')
        failure={'ok':False,'code':'upstream_rejected','http_status':403}
        with patch('engine_control.default_data_root',return_value=self.root/'store'), \
             patch('engine_control.ocx_service.start',return_value={'running':True}), \
             patch('probe.probe_snapshot',return_value=failure):
            result=activate_local_provider(p['id'])
        self.assertEqual((result['code'],result['detail'],result['http_status']),('probe_failed','upstream_rejected',403))
        self.assertIn('HTTP 403',result['message'])
        self.assertNotIn('synthetic-key',str(result))

    def test_disable_preserves_credential_and_blocks_the_active_engine_route(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        original = dict(self.keys)
        self.assertTrue(disable_provider(self.store, self.provider['id'], self.manager)['ok'])
        self.assertTrue(self.store.list()[0]['disabled'])
        self.assertTrue(self.config['providers'][self.route_name()]['disabled'])
        self.assertFalse(read_engine_routes(self.store, self.home)[1])
        self.assertEqual(self.keys, original)
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        self.store.enable_connection(self.provider['id'])
        self.assertFalse(self.store.list()[0]['disabled'])
        self.assertTrue(read_engine_routes(self.store, self.home)[1])

    def test_delete_active_requires_confirmation_and_removes_only_its_binding(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        other = self.store.save('Other', 'responses', 'https://other.invalid/v1', 'other', 'other-key')
        self.assertTrue(activate_provider(self.store, other['id'], self.manager, self.keyring)['ok'])
        denied = delete_provider(self.store, self.provider['id'], False, self.manager, self.keyring)
        self.assertEqual(denied['code'], 'connection_in_use')
        deleted = delete_provider(self.store, self.provider['id'], True, self.manager, self.keyring)
        self.assertTrue(deleted['ok'])
        self.assertFalse(deleted['credential_cleanup_pending'])
        self.assertFalse(any(value.get('defaultModel')=='fixture-beta' for value in self.config['providers'].values()))
        self.assertIsNone(self.store.get_engine_binding(self.alias))
        self.assertEqual(len(self.keys), 1)
        self.assertTrue(any(p['id'] == other['id'] for p in self.store.list()))

    def test_delete_rolls_back_profile_when_engine_refuses_and_records_cleanup_failure(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        self.manager.delete_provider = lambda name: False
        failed = delete_provider(self.store, self.provider['id'], True, self.manager, self.keyring)
        self.assertFalse(failed['ok'])
        self.assertTrue(read_engine_routes(self.store, self.home)[1])
        self.manager.delete_provider = lambda name: (self.config['providers'].pop(name), self.write(), True)[-1]
        failed_keyring = lambda action, account, value: False
        deleted = delete_provider(self.store, self.provider['id'], True, self.manager, failed_keyring)
        self.assertTrue(deleted['ok'])
        self.assertTrue(deleted['credential_cleanup_pending'])
        with closing(self.store._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM pending_credential_cleanup').fetchone()[0], 1)
        self.assertEqual(retry_credential_cleanup(self.store, self.manager, self.keyring), 0)
        self.assertEqual(len(self.keys), 0)

    def test_deleting_one_same_model_connection_preserves_the_other(self):
        first = activate_provider(self.store, self.provider['id'], self.manager, self.keyring)
        self.assertTrue(first['ok'])
        newer = self.store.save('Second', 'chat_completions', 'https://other.invalid/v1',
                                'fixture-beta', 'new-key')
        second = activate_provider(self.store, newer['id'], self.manager, self.keyring)
        self.assertTrue(second['ok'])
        self.assertNotEqual(first['alias'], second['alias'])
        binding = self.store.get_engine_binding(second['alias'])
        before = deepcopy(self.config['providers'][binding['account'].split('/', 1)[0]])
        self.assertEqual(delete_provider(self.store, self.provider['id'], False, self.manager, self.keyring)['code'],
                         'connection_in_use')
        self.assertTrue(delete_provider(self.store, self.provider['id'], True, self.manager, self.keyring)['ok'])
        self.assertEqual(self.config['providers'][binding['account'].split('/', 1)[0]], before)
        self.assertEqual(self.store.get_engine_binding(second['alias'])['id'], newer['id'])
        self.assertIsNone(self.store.get_engine_binding(first['alias']))
        self.assertEqual(len(self.keys), 1)

    def test_delete_recovers_when_engine_removes_provider_but_response_is_lost(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        remove = self.manager.delete_provider
        def lost_response(name):
            remove(name)
            raise OSError('synthetic lost response')
        self.manager.delete_provider = lost_response
        self.assertTrue(delete_provider(self.store, self.provider['id'], True, self.manager, self.keyring)['ok'])
        self.assertFalse(self.store.list())

    def test_delete_retries_after_process_stops_between_engine_and_database(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        self.manager.delete_provider(self.route_name())  # Simulate engine commit before process termination.
        self.assertIsNotNone(self.store.get_engine_binding(self.alias))
        result = delete_provider(self.store, self.provider['id'], True, self.manager, self.keyring)
        self.assertTrue(result['ok'])
        self.assertFalse(self.store.list())
        self.assertEqual(len(self.keys), 0)

    def test_committed_engine_change_with_lost_response_rolls_back(self):
        self.assertTrue(activate_provider(self.store, self.provider['id'], self.manager, self.keyring)['ok'])
        previous = deepcopy(self.config['providers'][self.route_name()])
        new = self.store.save('New External','chat_completions','https://other.invalid/v1',
                              'fixture-beta','new-synthetic-key')
        replace = self.manager.replace_provider
        def lost_response(name, value):
            result = replace(name, value)
            if value.get('apiKey') != previous.get('apiKey'):
                raise OSError('synthetic lost response after commit')
            return result
        self.manager.replace_provider = lost_response
        result = activate_provider(self.store, new['id'], self.manager, self.keyring)
        self.assertFalse(result['ok'])
        self.assertEqual(self.config['providers'][self.route_name()], previous)
        self.assertTrue(read_engine_routes(self.store,self.home)[1])
        self.assertEqual(len(self.keys),1)


if __name__ == '__main__': unittest.main()
