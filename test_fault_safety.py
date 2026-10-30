"""Fault injection for generation isolation, keyring timeouts and file/DB commit."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import unittest
from unittest.mock import patch
import test_engine_control as fixture
from engine_control import activate_provider,cleanup_unpublished_providers
from catalog import transactional_publish,recover_publication
from save_jobs import SaveJobs
from storage import ProfileStore
from front_proxy import named_route_ready


class FaultSafetyTests(unittest.TestCase):
    setUp=fixture.EngineControlTests.setUp
    tearDown=fixture.EngineControlTests.tearDown
    keyring=fixture.EngineControlTests.keyring
    write=fixture.EngineControlTests.write

    def test_sync_candidate_never_changes_the_previous_selected_engine(self):
        with patch('engine_control._requires_private_network',return_value=False):
            self.assertTrue(activate_provider(self.store,self.provider['id'],self.manager,self.keyring)['ok'])
            old_binding=self.store.get_engine_binding(self.alias);old_name=old_binding['account'].split('/')[0]
            old=deepcopy(self.config['providers'][old_name]);replace=self.manager.replace_provider
            def assert_before_commit(name,value):
                result=replace(name,value)
                self.assertNotEqual(name,old_name)
                self.assertEqual(self.store.get_engine_binding(self.alias),old_binding)
                self.assertEqual(self.config['providers'][old_name],old)
                self.assertTrue(named_route_ready(self.store,self.home,self.alias))
                return result
            self.manager.replace_provider=assert_before_commit
            self.assertTrue(activate_provider(self.store,self.provider['id'],self.manager,self.keyring)['ok'])

    def test_keyring_side_effect_then_timeout_is_compensated_and_tracked(self):
        def timeout(action,account,value):
            if action=='write':
                self.keyring(action,account,value)
                raise subprocess.TimeoutExpired('synthetic-keyring',4)
            return self.keyring(action,account,value)
        with patch('engine_control._requires_private_network',return_value=False):
            result=activate_provider(self.store,self.provider['id'],self.manager,timeout)
        self.assertFalse(result['ok']);self.assertTrue(result['rollback_ok'])
        self.assertEqual(self.keys,{})
        self.assertEqual(list((self.store.root/'staged-credentials').glob('*.json')),[])

    def test_timeout_with_failed_compensation_has_retryable_durable_journal(self):
        def timeout(action,account,value):
            if action=='write':self.keyring(action,account,value);raise subprocess.TimeoutExpired('synthetic-keyring',4)
            return False
        with patch('engine_control._requires_private_network',return_value=False):
            result=activate_provider(self.store,self.provider['id'],self.manager,timeout)
        self.assertFalse(result['ok']);self.assertFalse(result['rollback_ok'])
        self.assertEqual(len(self.keys),1)
        self.assertEqual(len(list((self.store.root/'staged-credentials').glob('*.json'))),1)
        self.assertEqual(cleanup_unpublished_providers(self.store,self.manager,self.keyring),0)
        self.assertEqual(self.keys,{})
        self.assertEqual(list((self.store.root/'staged-credentials').glob('*.json')),[])

    def test_catalog_publication_is_undone_if_sqlite_commit_fails(self):
        path=self.store.root/'catalog.json';path.write_bytes(b'old-catalog')
        real=sqlite3.connect
        class BrokenCommit(sqlite3.Connection):
            def commit(connection):
                if path.read_bytes()==b'new-catalog': raise sqlite3.OperationalError('synthetic commit failure')
                return super().commit()
        def connect(*args,**kwargs):return real(*args,**dict(kwargs,factory=BrokenCommit))
        before=self.store.list()
        with patch('storage.sqlite3.connect',side_effect=connect):
            with self.assertRaises(sqlite3.Error):
                with self.store.atomic():
                    self.store.save('Changed','chat_completions','https://example.invalid/v1','fixture-beta','',self.provider['id'])
                    transactional_publish(self.store,lambda:path.write_bytes(b'new-catalog'))
        self.assertEqual(path.read_bytes(),b'old-catalog')
        self.assertEqual(self.store.list(),before)
        self.assertFalse((self.store.root/'catalog-publication.json').exists())

    def test_process_exit_before_catalog_db_commit_recovers_previous_file(self):
        root=self.root/'crashed';root.mkdir()
        code="""import os,sys;from pathlib import Path;from storage import ProfileStore;from catalog import transactional_publish
root=Path(sys.argv[1]);store=ProfileStore(root);store.list();path=root/'catalog.json';path.write_bytes(b'old')
with store.atomic():
 transactional_publish(store,lambda:path.write_bytes(b'new'))
 os._exit(0)
"""
        subprocess.run([sys.executable,'-B','-c',code,str(root)],cwd=Path(__file__).parent,check=True)
        self.assertEqual((root/'catalog.json').read_bytes(),b'new')
        recover_publication(ProfileStore(root))
        self.assertEqual((root/'catalog.json').read_bytes(),b'old')
        self.assertFalse((root/'catalog-publication.json').exists())

    def test_cancel_reports_failed_cleanup_and_retry_removes_owned_candidates(self):
        entered=threading.Event();release=threading.Event()
        replace=self.manager.replace_provider;delete=self.manager.delete_provider
        def pause(name,value):
            result=replace(name,value);entered.set();release.wait(4);return result
        self.manager.replace_provider=pause;self.manager.delete_provider=lambda name:False
        jobs=SaveJobs(store_factory=lambda:ProfileStore(self.store.root),manager_factory=lambda root:self.manager,
                      keyring_fn=self.keyring,publish_fn=lambda *a:None)
        try:
            with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
                start=jobs.start(client_job_id=str(uuid.uuid4()),name='Cancel',protocol='responses',base_url='https://fixture.invalid/v1',model='native-alpha',models=['native-alpha','native-beta'],key='synthetic-key')
                job=jobs.jobs[start['job_id']];thread=job['thread']
                end=time.monotonic()+5
                while jobs.status(start['job_id'])['state']!='ready' and time.monotonic()<end: time.sleep(.01)
                self.assertTrue(jobs.commit(start['job_id'])['ok'])
                self.assertTrue(entered.wait(2));self.assertTrue(jobs.cancel(start['job_id'])['ok']);release.set();thread.join(5)
            status=jobs.status(start['job_id'])
            self.assertEqual(status['state'],'cancelled',status)
            self.assertTrue(status['cleanup_pending'])
            self.assertIn('清理未完成',status['message'])
            self.manager.delete_provider=delete
            self.assertEqual(cleanup_unpublished_providers(self.store,self.manager,self.keyring),0)
            self.assertEqual(set(self.config['providers']),{'openai'})
            self.assertEqual(self.keys,{})
        finally:release.set()


if __name__=='__main__':unittest.main()
