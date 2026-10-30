import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from guard_verification import PLUGIN, OLD_PLUGIN, SUFFIXES, REQUIRED_FILES, check_hooks, select_desktop_executable, guard_verified


class GuardVerificationTests(unittest.TestCase):
    def test_guard_verification_rejects_invalid_source_version_before_querying_host(self):
        with patch('guard_verification.json.loads', return_value={'version':'../../outside'}), \
             patch('guard_verification.query_host_hooks') as query:
            self.assertFalse(guard_verified(Path('.'), Path('.')))
            query.assert_not_called()

    def test_desktop_executable_tracks_running_version_and_rejects_ambiguity(self):
        first=Path('first/codex.exe'); second=Path('second/codex.exe')
        self.assertEqual(select_desktop_executable([first], []), first)
        self.assertEqual(select_desktop_executable([first, second], [second]), second)
        with self.assertRaises(ValueError): select_desktop_executable([first, second], [])
        with self.assertRaises(ValueError): select_desktop_executable([first, second], [first, second])

    def test_requires_matching_trusted_hooks_and_no_old_enabled_guard(self):
        with tempfile.TemporaryDirectory(prefix='hub-guard-verify-') as temp:
            root=Path(temp); inventory={'version':'0.6.6','files':{}}
            for name in REQUIRED_FILES:
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('fixture',encoding='utf-8')
                inventory['files'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
            file=root/'hooks/hooks.json'
            hooks=[{'pluginId':PLUGIN,'key':PLUGIN+':hooks/hooks.json:'+suffix,'enabled':True,
                    'trustStatus':'trusted','sourcePath':str(file)} for suffix in SUFFIXES]
            response={'data':[{'hooks':hooks}]}
            self.assertTrue(check_hooks(response,root,inventory))
            incomplete={'version':'0.6.6','files':{'hooks/hooks.json':inventory['files']['hooks/hooks.json']}}
            self.assertFalse(check_hooks(response,root,incomplete))
            hooks[0]['trustStatus']='untrusted'; self.assertFalse(check_hooks(response,root,inventory))
            hooks[0]['trustStatus']='trusted'; hooks.append({'pluginId':OLD_PLUGIN,'enabled':True})
            self.assertFalse(check_hooks(response,root,inventory))
            self.assertTrue(check_hooks(response,root,inventory,allow_old=True))


if __name__ == '__main__': unittest.main()
