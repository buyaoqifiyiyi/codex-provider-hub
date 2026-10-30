import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from config_migration import apply_config, plan_config, plan_per_model_context, apply_per_model_context
from storage import ProfileStore, _dpapi

TOKEN = 'synthetic_local_capability_0123456789'


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI and ACLs are required')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.native_patch=patch('config_migration.native_model_ids',return_value={'native-fixture'})
        self.native_patch.start()
        self.addCleanup(self.native_patch.stop)

    def test_remove_global_window_only_when_catalog_preserves_existing_values(self):
        catalog = Path('C:/fixture/catalog.json')
        source = ('model_provider="custom"\nmodel_catalog_json=' + repr(str(catalog)) +
                  '\nmodel_context_window=1000000\nmodel="native-fixture"\n')
        matching = {'models': [{'slug':'native-fixture', 'context_window':1000000},
                               {'slug':'external-fixture', 'context_window':1000000}]}
        result = plan_per_model_context(source, matching, catalog)
        parsed = tomllib.loads(result)
        self.assertNotIn('model_context_window', parsed)
        self.assertEqual(parsed['model'], 'native-fixture')
        with self.assertRaises(ValueError):
            plan_per_model_context(source, {'models':[{'slug':'external-fixture', 'context_window':32768}]}, catalog)

    def test_per_model_migration_creates_encrypted_backup_and_preserves_other_settings(self):
        with tempfile.TemporaryDirectory(prefix='hub-window-migration-') as tmp:
            root=Path(tmp)/'CodexProviderHub'; home=Path(tmp)/'codex'; root.mkdir(); home.mkdir()
            catalog=root/'catalog.json'
            catalog.write_text('{"models":[{"slug":"native-fixture","context_window":1000000}]}', encoding='utf-8')
            config=home/'config.toml'
            config.write_text('model_provider="custom"\nmodel_catalog_json='+repr(str(catalog))+
                              '\nmodel_context_window=1000000\nmodel="native-fixture"\n', encoding='utf-8')
            old=config.read_bytes()
            result=apply_per_model_context(root,home)
            self.assertTrue(result['changed'])
            self.assertEqual(_dpapi(Path(result['backup']).read_bytes(), decrypt=True), old)
            self.assertNotIn('model_context_window',tomllib.loads(config.read_text(encoding='utf-8')))
            self.assertFalse(apply_per_model_context(root,home)['changed'])

    def test_preserves_unrelated_settings_and_encrypts_rollback_snapshot(self):
        with tempfile.TemporaryDirectory(prefix='hub-config-test-') as tmp:
            root=Path(tmp)/'CodexProviderHub'; home=Path(tmp)/'codex'; root.mkdir();home.mkdir()
            (root/'shared').mkdir();(root/'shared'/'jev_hub.py').write_text('# fixture',encoding='utf-8')
            old_jev=root/'previous'/'server.py';old_jev.parent.mkdir()
            old_jev.write_bytes((Path(__file__).parent/'jev_tools'/'server.py').read_bytes())
            (root/'catalog.json').write_text('{"models":[{"slug":"native-fixture","visibility":"list"}]}',encoding='utf-8')
            (root/'gateway-capability.bin').write_bytes(_dpapi(TOKEN.encode()))
            store=ProfileStore(root)
            jev=store.save('Jev','system_one','https://example.invalid/v1/systemone','fixture-jev','synthetic-key')
            store.set_jev_profile(jev['id'])
            source=('# keep this comment\nmodel_provider="custom"\nmodel="native-fixture"\n'
                    'model_catalog_json="old.json"\n[model_providers.custom]\nname="Fixture"\n'
                    'base_url="http://127.0.0.1:24568/v1"\nwire_api="responses"\n'
                    'requires_openai_auth=true\nsupports_websockets=false\n[features]\nmemories=true\n'
                    '[plugins."subagent-model-guard@subagent-model-guard-local"]\nenabled=true\n'
                    '[mcp_servers.legacy-jev]\ncommand="python"\nargs=[' + repr(str(old_jev)).replace('\\\\','\\') + ']\n')
            target=home/'config.toml'; target.write_text(source,encoding='utf-8')
            original_bytes=target.read_bytes()
            state={'running':True,'port':23456,'external_route_ready':True,'official_route_ready':True}
            with patch('config_migration.ocx_service.status',return_value=state), patch('config_migration.guard_verified',return_value=True), patch('config_migration.startup_verified',return_value=True):
                result=apply_config(root,home)
                second=apply_config(root,home)
            parsed=tomllib.loads(target.read_text(encoding='utf-8'))
            self.assertTrue(result['changed']); self.assertFalse(second['changed'])
            self.assertEqual(parsed['model_providers']['custom']['base_url'],'http://127.0.0.1:23456/v1')
            self.assertEqual(parsed['model'], 'native-fixture'); self.assertTrue(parsed['features']['memories'])
            self.assertFalse(parsed['plugins']['subagent-model-guard@subagent-model-guard-local']['enabled'])
            self.assertIn('# keep this comment',target.read_text(encoding='utf-8'))
            self.assertEqual(parsed['mcp_servers']['legacy-jev']['args'],[str(root/'shared/jev_hub.py')])
            backup=Path(result['backup']).read_bytes()
            self.assertNotIn(original_bytes,backup)
            self.assertEqual(_dpapi(backup,decrypt=True),original_bytes)

    def test_external_and_unrecognized_jev_paths_are_refused(self):
        with tempfile.TemporaryDirectory(prefix='hub-plan-test-') as tmp:
            root=Path(tmp); (root/'catalog.json').write_text('{"models":[]}',encoding='utf-8')
            source='model_provider="custom"\n[model_providers.custom]\nbase_url="https://external.invalid/v1"\n'
            with self.assertRaises(ValueError): plan_config(source,root,23456,TOKEN,False)

    def test_integrated_jev_does_not_require_a_legacy_mcp_entry(self):
        with tempfile.TemporaryDirectory(prefix='hub-integrated-jev-') as tmp:
            root=Path(tmp); (root/'catalog.json').write_text('{"models":[{"slug":"native-fixture"}]}',encoding='utf-8')
            source=('model_provider="custom"\nmodel="native-fixture"\n'
                    '[model_providers.custom]\nbase_url="http://127.0.0.1:24568/v1"\n')
            parsed=tomllib.loads(plan_config(source,root,23456,TOKEN,True))
            self.assertNotIn('legacy-jev',parsed.get('mcp_servers',{}))
            self.assertEqual(parsed['model_providers']['custom']['base_url'],'http://127.0.0.1:23456/v1')

    def test_unrelated_mcp_entry_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='hub-unknown-jev-') as tmp:
            root=Path(tmp); (root/'catalog.json').write_text('{"models":[{"slug":"native-fixture"}]}',encoding='utf-8')
            source=('model_provider="custom"\nmodel="native-fixture"\n'
                    '[model_providers.custom]\nbase_url="http://127.0.0.1:24568/v1"\n'
                    '[mcp_servers.unrelated]\ncommand="python"\nargs=["unknown.py"]\n')
            result=tomllib.loads(plan_config(source,root,23456,TOKEN,True))
            self.assertEqual(result['mcp_servers']['unrelated']['args'],['unknown.py'])

    def test_official_direct_migrates_only_when_unambiguous(self):
        with tempfile.TemporaryDirectory(prefix='hub-official-plan-') as tmp:
            root=Path(tmp); (root/'catalog.json').write_text('{"models":[{"slug":"native-fixture"}]}',encoding='utf-8')
            source=('model_provider="custom"\nmodel="native-fixture"\n'
                    '[model_providers.custom]\nname="OpenAI"\nwire_api="responses"\nrequires_openai_auth=true\n')
            with patch.dict(os.environ, {'OPENAI_BASE_URL':'', 'OPENAI_API_BASE':''}):
                result=tomllib.loads(plan_config(source,root,23456,TOKEN,False))
                self.assertEqual(result['model_providers']['custom']['base_url'],'http://127.0.0.1:23456/v1')
                with self.assertRaises(ValueError):plan_config('openai_base_url="https://other.invalid"\n'+source,root,23456,TOKEN,False)
                with self.assertRaises(ValueError):plan_config(source.replace('name="OpenAI"','name="other"'),root,23456,TOKEN,False)

    def test_auth_overrides_and_active_profile_cannot_be_silently_carried(self):
        with tempfile.TemporaryDirectory(prefix='hub-auth-plan-') as tmp:
            root=Path(tmp); (root/'catalog.json').write_text('{"models":[{"slug":"native-fixture"}]}',encoding='utf-8')
            prefix='model_provider="custom"\nmodel="native-fixture"\n'
            provider='[model_providers.custom]\nbase_url="http://127.0.0.1:24568/v1"\n'
            for extra in ('env_key="OLD_KEY"\n','experimental_bearer_token="synthetic"\n',
                          'http_headers={Authorization="Bearer synthetic"}\n',
                          'env_http_headers={"X-Provider-Hub-Token"="OLD_TOKEN"}\n'):
                with self.subTest(extra=extra),self.assertRaises(ValueError):
                    plan_config(prefix+provider+extra,root,23456,TOKEN,False)
            with self.assertRaises(ValueError):
                plan_config('profile="old"\n'+prefix+provider,root,23456,TOKEN,False)


if __name__ == '__main__': unittest.main()
