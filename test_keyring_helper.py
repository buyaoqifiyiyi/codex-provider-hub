import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest
import uuid
import tempfile

ROOT = Path(__file__).resolve().parent
NODE = Path(shutil.which('node') or '')


@unittest.skipUnless(NODE.is_file(), 'Node test runtime is unavailable')
class KeyringHelperTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='hub-fake-keyring-')
        self.addCleanup(temp.cleanup)
        self.binding = Path(temp.name) / 'fake-keyring.cjs'
        # Run the real stdin-only helper against a disposable credential backend.
        self.binding.write_text("""const fs=require('node:fs');
const file=__filename+'.json';
function read(){return fs.existsSync(file)?JSON.parse(fs.readFileSync(file,'utf8')):{};}
exports.Entry=class {
 constructor(service,account){this.key=service+'/'+account;}
 getPassword(){return read()[this.key]??null;}
 setPassword(value){const db=read();db[this.key]=value;fs.writeFileSync(file,JSON.stringify(db));}
 deletePassword(){const db=read();delete db[this.key];fs.writeFileSync(file,JSON.stringify(db));return true;}
};""",encoding='utf-8')

    def test_secret_never_returns_and_collision_is_refused(self):
        account = 'hub-test/' + str(uuid.uuid4())
        secret = 'synthetic-fixture-secret-' + uuid.uuid4().hex
        env = dict(os.environ, CODEX_PROVIDER_HUB_KEYRING=str(self.binding))
        def call(action, value=secret):
            process = subprocess.run([str(NODE), str(ROOT / 'keyring_helper.cjs')], input=json.dumps({
                'action': action, 'account': account, 'secret': value}), capture_output=True, text=True,
                encoding='utf-8', env=env, timeout=5)
            self.assertNotIn(secret, process.stdout + process.stderr)
            return json.loads(process.stdout)
        try:
            self.assertTrue(call('write')['ok'])
            self.assertTrue(call('verify')['ok'])
            self.assertFalse(call('write', 'different-synthetic-secret')['ok'])
            self.assertTrue(call('verify')['ok'])
        finally:
            self.assertTrue(call('discard')['ok'])

    def test_generated_account_formats_with_isolated_helper(self):
        from storage import valid_engine_account
        prefixes=['fixture-route','hub-'+uuid.uuid4().hex,'hub-'+uuid.uuid4().hex+'-'+uuid.uuid4().hex[:12],
                  'hub-'+uuid.uuid4().hex[:16]+'-'+uuid.uuid4().hex[:12]]
        for prefix in prefixes:
            account=prefix+'/'+str(uuid.uuid4())
            secret='synthetic-format-check-'+uuid.uuid4().hex
            env=dict(os.environ,CODEX_PROVIDER_HUB_KEYRING=str(self.binding))
            def call(action):
                run=subprocess.run([str(NODE),str(ROOT/'keyring_helper.cjs')],input=json.dumps({'action':action,'account':account,'secret':secret}),
                                   capture_output=True,text=True,encoding='utf-8',env=env,timeout=5)
                self.assertNotIn(secret,run.stdout+run.stderr)
                return json.loads(run.stdout)
            with self.subTest(prefix=prefix):
                self.assertTrue(valid_engine_account(account))
                try:
                    self.assertTrue(call('write')['ok'])
                    self.assertTrue(call('verify')['ok'])
                finally:self.assertTrue(call('discard')['ok'])

    def test_invalid_account_formats_are_rejected_before_native_credential_access(self):
        from storage import valid_engine_account
        suffix='/'+str(uuid.uuid4())
        invalid=['', 'a'*129, '../hub-test', 'bad space', '/nested', '-leading', 'line\nbreak']
        accounts=[prefix+suffix for prefix in invalid]+['hub-test/not-a-uuid','hub-test/'+str(uuid.uuid4())+'\n']
        for account in accounts:
            with self.subTest(account=account):
                self.assertFalse(valid_engine_account(account))
                run=subprocess.run([str(NODE),str(ROOT/'keyring_helper.cjs')],input=json.dumps({'action':'discard','account':account}),
                                   capture_output=True,text=True,encoding='utf-8',env=dict(os.environ,CODEX_PROVIDER_HUB_KEYRING=str(self.binding)),timeout=5)
                self.assertFalse(json.loads(run.stdout)['ok'])


if __name__ == '__main__': unittest.main()
