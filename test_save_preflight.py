import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

# Import-time defaults must also stay inside a disposable environment.
with tempfile.TemporaryDirectory(prefix='hub-neutral-import-') as import_root:
    with patch.dict(os.environ, {'LOCALAPPDATA':import_root, 'CODEX_HOME':str(Path(import_root)/'codex'), 'HF_HUB_OFFLINE':'1'}):
        import mcp_server
from storage import ProfileStore


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI is required')
class SavePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='hub-save-preflight-')
        self.env = patch.dict(os.environ, {'LOCALAPPDATA':self.temp.name})
        self.env.start()
        self.store = ProfileStore()
        mcp_server._pretests.clear()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def save(self, **overrides):
        args={'name':'Fixture','protocol':'responses','base_url':'https://fixture.invalid/v1',
              'model':'fixture','key':'synthetic-key'}
        args.update(overrides)
        return mcp_server.save_provider(**args)

    def test_failed_new_or_updated_connection_never_writes_profiles_or_credentials(self):
        original=self.store.save('Existing','responses','https://fixture.invalid/v1','fixture','old-key')
        with patch('probe.probe_snapshot',return_value={'ok':False,'code':'upstream_rejected','http_status':401}), \
             patch('probe.discover_capabilities') as metadata:
            self.assertFalse(self.save()['ok'])
            self.assertFalse(self.save(name='Changed',provider_id=original['id'],model='new-model',key='new-key')['ok'])
            metadata.assert_not_called()
        rows=self.store.list()
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['name'],'Existing')
        self.assertEqual(self.store.get_secret(original['id']),'old-key')

    def test_failure_diagnostics_reach_the_ui_without_sensitive_response_text(self):
        cases=[({'ok':False,'code':'upstream_rejected','http_status':401},'HTTP 401'),
               ({'ok':False,'code':'response_protocol_unsupported','detail':'unsupported_field'},'返回格式'),
               ({'ok':False,'code':'output_limit'},'长度上限'),
               ({'ok':False,'code':'network_timeout'},'超时')]
        for failed, expected in cases:
            with self.subTest(code=failed['code']), patch('probe.probe_snapshot',return_value=failed):
                result=self.save(key='synthetic-secret')
                self.assertFalse(result['ok'])
                self.assertEqual(result['detail'],failed['code'])
                self.assertIn(expected,result['message'])
                self.assertIn('未保存或修改连接',result['message'])
                self.assertNotIn('synthetic-secret',str(result))
        self.assertEqual(self.store.list(),[])

    def test_success_persists_exact_detected_values_and_one_time_pretest_receipt(self):
        found={'context_window':128000,'reasoning_levels':['medium','high'],'default_effort':'high'}
        with patch('probe.probe_snapshot',return_value={'ok':True}) as test, \
             patch('probe.discover_capabilities',return_value=found):
            result=self.save()
        self.assertTrue(result['ok'])
        self.assertEqual(result['detected']['source'],'服务商模型目录')
        profile=self.store.list()[0]
        self.assertEqual(profile['context_window'],128000)
        self.assertEqual(profile['reasoning_levels'],['medium','high'])
        self.assertEqual(profile['default_effort'],'high')
        self.assertEqual(result['detected']['context_window'],128000)
        self.assertEqual(test.call_count,1)
        with patch('engine_control.activate_local_provider',return_value={'ok':True}) as activate:
            mcp_server.activate_connection(profile['id'])
            first=activate.call_args.kwargs['pretested_revision']
            mcp_server.activate_connection(profile['id'])
            second=activate.call_args.kwargs['pretested_revision']
        self.assertIsInstance(first,str)
        self.assertIsNone(second)

    def test_missing_metadata_is_explained_and_manual_settings_are_not_overwritten(self):
        with patch('probe.probe_snapshot',return_value={'ok':True}), \
             patch('probe.discover_capabilities',return_value={}):
            result=self.save(context_window=64000,reasoning_levels=['high'],default_effort='high')
        self.assertTrue(result['ok'])
        self.assertEqual(self.store.list()[0]['context_window'],64000)
        self.assertEqual(len(result['warnings']),2)

    def test_unknown_capabilities_stay_unset_in_saved_profile(self):
        with patch('probe.probe_snapshot',return_value={'ok':True}), \
             patch('probe.discover_capabilities',return_value={}):
            result=self.save()
        self.assertTrue(result['ok'],result)
        profile=self.store.list()[0]
        for field in ('context_window','compact_limit','reasoning_levels','default_effort'):
            self.assertIsNone(profile[field],field)
        self.assertEqual(len(result['warnings']),2)
        self.assertNotIn('synthetic-key',str(result))

    def test_changed_origin_requires_new_key_before_network(self):
        original=self.store.save('Existing','responses','https://fixture.invalid/v1','fixture','old-key')
        with patch('probe.probe_snapshot') as test:
            result=self.save(provider_id=original['id'],base_url='https://different.invalid/v1',key='')
            self.assertFalse(result['ok'])
            test.assert_not_called()
        self.assertEqual(self.store.get_secret(original['id']),'old-key')

    def test_concurrent_edit_during_probe_is_not_overwritten(self):
        original=self.store.save('Existing','responses','https://fixture.invalid/v1','fixture','old-key')
        def concurrent(route,key,effort):
            self.store.save('Other edit','responses','https://fixture.invalid/v1','fixture','new-key',original['id'])
            return {'ok':True}
        with patch('probe.probe_snapshot',side_effect=concurrent), patch('probe.discover_capabilities',return_value={}):
            result=self.save(provider_id=original['id'],model='new-model',key='')
        self.assertFalse(result['ok'])
        self.assertIn('发生变化',result['message'])
        self.assertEqual(self.store.list()[0]['name'],'Other edit')
        self.assertEqual(self.store.get_secret(original['id']),'new-key')

    def test_existing_provider_rename_does_not_send_a_probe_or_metadata_request(self):
        original=self.store.save('Existing','responses','https://fixture.invalid/v1','fixture','old-key')
        with patch('probe.probe_snapshot') as test, patch('probe.discover_capabilities') as metadata:
            result=self.save(provider_id=original['id'],name='Renamed',key='')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['tested_models'], [])
        test.assert_not_called();metadata.assert_not_called()
        self.assertEqual(self.store.list()[0]['name'],'Renamed')
        self.assertEqual(self.store.model_ids(original['id']),['fixture'])

    def test_structured_and_subscription_failures_also_prevent_save(self):
        with patch('probe.probe_jev',return_value={'ok':False}), \
             patch('probe.probe_official',return_value={'ok':False,'code':'subscription_login_required'}):
            self.assertFalse(self.save(protocol='system_one',base_url='https://fixture.invalid/v1/systemone')['ok'])
            self.assertFalse(self.save(protocol='codex_subscription',base_url='',model='native-alpha',key='')['ok'])
        self.assertEqual(self.store.list(),[])


if __name__ == '__main__': unittest.main()
