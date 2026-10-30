"""Asynchronous state-machine checks; all data and providers are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid
from unittest.mock import patch
import test_engine_control as fixture
from catalog import compile_catalog
from storage import ProfileStore, validate_models
from save_jobs import SaveJobs
from test_neutral_python_compat import alias_for
from front_proxy import named_route_ready


class SaveJobTests(unittest.TestCase):
    setUp=fixture.EngineControlTests.setUp
    tearDown=fixture.EngineControlTests.tearDown
    keyring=fixture.EngineControlTests.keyring
    write=fixture.EngineControlTests.write

    def jobs(self):
        def publish(store,manager):
            incoming=[{'slug':name+'/'+model.replace('/','-'),'context_window':32768,'supported_reasoning_levels':[{'effort':'high'}]}
                      for name,p in manager.snapshot()['providers'].items() for model in p.get('models',[])]
            result=compile_catalog({'models':[]},{'models':incoming},store)
            (store.root/'catalog.json').write_text(json.dumps(result),encoding='utf-8')
        return SaveJobs(store_factory=lambda:ProfileStore(self.store.root),manager_factory=lambda root:self.manager,
                        keyring_fn=self.keyring,publish_fn=publish)

    def start(self,jobs,**overrides):
        args={'client_job_id':str(uuid.uuid4()),'name':'Job','protocol':'responses','base_url':'https://fixture.invalid/v1','model':'native-alpha',
              'models':['native-alpha','fixture-beta'],'key':'synthetic-job-secret'}
        args.update(overrides)
        return jobs.start(**args)

    def wait_ready(self,jobs,start):
        end=time.monotonic()+10
        while time.monotonic()<end:
            status=jobs.status(start['job_id'])
            if status['state'] in {'ready','failed','cancelled'}: return status
            time.sleep(.01)
        self.fail('job did not become ready')

    def authorize(self,jobs,start):
        status=self.wait_ready(jobs,start)
        if status['state']=='ready': self.assertTrue(jobs.commit(start['job_id'])['ok'])

    def finish(self,jobs,start):
        if jobs.status(start['job_id'])['state'] in {'queued','testing','ready'}: self.authorize(jobs,start)
        job=jobs.jobs[start['job_id']]
        thread=job.get('thread')
        if thread: thread.join(15);self.assertFalse(thread.is_alive(),'job did not terminate')
        return jobs.status(start['job_id'])

    def capture_payload(self,jobs):
        captured=[];run=jobs._run
        def capture(job,payload):captured.append(payload);return run(job,payload)
        jobs._run=capture
        return captured

    def test_rename_saved_provider_never_retests_existing_models(self):
        p=self.store.save('Existing','responses','https://fixture.invalid/v1','model-a','saved-key',
                          models=['model-a','model-b'])
        jobs=self.jobs()
        with patch('probe.probe_snapshot') as short_test, patch('probe.discover_capabilities') as metadata, \
             patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs,provider_id=p['id'],name='Renamed',
                              model='model-a',models=['model-a','model-b'],key=''))
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual(done['result']['tested_models'],[])
        self.assertEqual(done['short_tests_started'],0)
        short_test.assert_not_called();metadata.assert_not_called()
        saved=next(row for row in self.store.list() if row['id']==p['id'])
        self.assertEqual(saved['name'],'Renamed')
        self.assertEqual(saved['models'],['model-a','model-b'])

    def test_only_new_models_are_tested_and_failure_does_not_change_existing(self):
        p=self.store.save('Existing','responses','https://fixture.invalid/v1','model-a','saved-key',
                          models=['model-a','model-b'])
        jobs=self.jobs()
        with patch('probe.probe_snapshot',return_value={'ok':True}) as short_test, \
             patch('probe.discover_capabilities',return_value={}) as metadata, \
             patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs,provider_id=p['id'],name='Existing',model='model-a',
                              models=['model-a','model-b','model-c'],key=''))
        self.assertEqual(done['state'],'succeeded',done)
        self.assertEqual(done['result']['tested_models'],['model-c'])
        self.assertEqual(short_test.call_count,1)
        self.assertEqual(short_test.call_args.args[0]['model'],'model-c')
        self.assertEqual(metadata.call_args.args[0]['model'],'model-c')
        before=self.store.list()
        with patch('probe.probe_snapshot',return_value={'ok':False,'code':'upstream_rejected'}) as short_test, \
             patch('probe.discover_capabilities') as metadata:
            done=self.finish(jobs,self.start(jobs,provider_id=p['id'],name='Changed',model='model-a',
                              models=['model-a','model-b','model-c','model-d'],key=''))
        self.assertEqual(done['state'],'failed')
        self.assertEqual(short_test.call_args.args[0]['model'],'model-d')
        metadata.assert_not_called()
        self.assertEqual(self.store.list(),before)

    def test_existing_key_change_does_not_retest_or_claim_new_test_receipts(self):
        p=self.store.save('Existing','responses','https://fixture.invalid/v1','model-a','old-key')
        jobs=self.jobs()
        with patch('probe.probe_snapshot') as short_test, patch('probe.discover_capabilities') as metadata, \
             patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs,provider_id=p['id'],name='Existing',
                              model='model-a',models=['model-a'],key='new-key'))
        self.assertEqual(done['state'],'succeeded',done)
        short_test.assert_not_called();metadata.assert_not_called()
        self.assertEqual(done['result']['tested_models'],[])
        self.assertEqual(done['result']['probe_receipts'],[])
        self.assertTrue(done['result']['warnings'])
        self.assertEqual(self.store.get_secret(p['id']),'new-key')

    def test_save_allows_a_slow_first_response_and_remains_cancellable(self):
        import probe
        jobs=self.jobs();calls=[]
        def bounded(operation,deadline,cancel_check=None):
            calls.append(deadline-time.monotonic())
            if len(calls)==1 and calls[0] < 60:
                raise probe.ReadDeadline('slow response')
            return operation()
        with patch('probe.bounded_read',side_effect=bounded), \
             patch('probe.probe_snapshot',return_value={'ok':True}), \
             patch('probe.discover_capabilities',return_value={}):
            started=self.start(jobs,models=['native-alpha'])
            state=self.wait_ready(jobs,started)
            self.assertEqual(state['state'],'ready',state)
            self.assertTrue(jobs.cancel(started['job_id'])['ok'])
            thread=jobs.jobs[started['job_id']].get('thread')
            if thread: thread.join(2)
            self.assertEqual(jobs.status(started['job_id'])['state'],'cancelled')

    def test_ready_ttl_cancels_and_releases_sensitive_payload(self):
        jobs=self.jobs();captured=self.capture_payload(jobs);before=self.store.list()
        with patch('save_jobs.READY_TTL_SECONDS',.15),patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}):
            started=self.start(jobs);ready=self.wait_ready(jobs,started)
            self.assertEqual(ready['state'],'ready',ready)
            self.assertGreater(ready['ready_expires_at'],time.time())
            thread=jobs.jobs[started['job_id']]['thread'];thread.join(2)
            self.assertFalse(thread.is_alive())
        done=jobs.status(started['job_id'])
        self.assertEqual(done['state'],'cancelled',done)
        self.assertIn('过期',done['message'])
        self.assertEqual(captured,[{}])
        self.assertEqual(self.store.list(),before)
        self.assertEqual(self.keys,{})
        self.assertEqual(jobs.commit(started['job_id'])['state'],'cancelled')
        self.assertEqual(self.jobs().status(started['job_id'])['state'],'cancelled')

    def test_cancel_ready_immediately_wakes_long_ttl_wait(self):
        jobs=self.jobs();captured=self.capture_payload(jobs)
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}):
            started=self.start(jobs);self.assertEqual(self.wait_ready(jobs,started)['state'],'ready')
            thread=jobs.jobs[started['job_id']]['thread'];now=time.monotonic()
            self.assertTrue(jobs.cancel(started['job_id'])['ok'])
            thread.join(1);self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic()-now,1)
        self.assertEqual(jobs.status(started['job_id'])['state'],'cancelled')
        self.assertEqual(captured,[{}])

    def test_commit_checks_expiration_even_before_waiter_runs(self):
        jobs=self.jobs();before=self.store.list()
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}):
            started=self.start(jobs);self.assertEqual(self.wait_ready(jobs,started)['state'],'ready')
            job=jobs.jobs[started['job_id']];thread=job['thread']
            with job['lock']:
                job['ready_deadline']=time.monotonic()-1
                self.assertEqual(jobs.commit(started['job_id'])['code'],'job_expired')
            thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual(jobs.status(started['job_id'])['state'],'cancelled')
        self.assertEqual(self.store.list(),before)

    def test_lost_start_reply_and_duplicate_uuid_never_authorize_save(self):
        jobs=self.jobs();ident=str(uuid.uuid4());before=self.store.list()
        with patch('probe.probe_snapshot',return_value={'ok':True}) as probes,patch('probe.discover_capabilities',return_value={}):
            self.start(jobs,client_job_id=ident)  # Discard the response entirely.
            status=self.wait_ready(jobs,{'job_id':ident})
            self.assertEqual(status['state'],'ready')
            self.assertEqual(self.store.list(),before)
            self.assertEqual(self.keys,{})
            retry=self.start(jobs,client_job_id=ident,name='Ignored retry data')
            self.assertEqual(retry['job_id'],ident)
            self.assertEqual(retry['state'],'ready')
            self.assertEqual(probes.call_count,2)
            self.assertEqual(self.jobs().status(ident)['state'],'detached')
            self.assertFalse(self.jobs().commit(ident)['ok'])
            self.assertTrue(jobs.cancel(ident)['ok'])
            done=self.finish(jobs,{'job_id':ident})
            self.assertEqual(done['state'],'cancelled')
            self.assertEqual(self.store.list(),before)
            self.assertEqual(self.start(jobs,client_job_id=ident)['state'],'cancelled')

    def test_commit_reply_loss_and_retries_save_exactly_once(self):
        jobs=self.jobs()
        with patch('probe.probe_snapshot',return_value={'ok':True}) as probes,patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
            started=self.start(jobs);self.assertEqual(self.wait_ready(jobs,started)['state'],'ready')
            jobs.commit(started['job_id'])  # Discard commit response too.
            done=self.finish(jobs,started)
            self.assertEqual(done['state'],'succeeded',done)
            self.assertEqual(jobs.commit(started['job_id'])['state'],'succeeded')
            retry=self.start(jobs,client_job_id=started['job_id'])
            self.assertEqual(retry['state'],'succeeded')
            recovered=self.jobs().status(started['job_id'])
            self.assertEqual(recovered['result'],done['result'])
            self.assertEqual(probes.call_count,2)
            self.assertEqual(len([p for p in self.store.list() if p['name']=='Job']),1)

    def test_5000_models_register_before_candidate_validation_or_credentials(self):
        jobs=self.jobs();entered=threading.Event();release=threading.Event()
        def slow(*args):
            entered.set();release.wait(5)
            raise ValueError('synthetic candidate rejection')
        jobs.candidate_fn=slow
        models=['model'+str(i) for i in range(5000)]
        try:
            before=time.monotonic()
            started=self.start(jobs,model=models[0],models=models)
            self.assertLess(time.monotonic()-before,1.5)
            self.assertTrue(entered.wait(2))
            self.assertEqual(started['total'],5000)
            self.assertEqual(jobs.commit(started['job_id'])['code'],'job_not_ready')
            self.assertTrue(jobs.cancel(started['job_id'])['ok'])
            release.set();done=self.finish(jobs,started)
            self.assertIn(done['state'],{'cancelled','failed'})
        finally:release.set()

    def test_5000_model_configuration_uses_bounded_database_opens(self):
        jobs=self.jobs();entered=threading.Event();release=threading.Event()
        models=['model'+str(i) for i in range(5000)]
        original=ProfileStore._connect;opens=[]
        def connect(store):opens.append(1);return original(store)
        def slow(*args):entered.set();release.wait(5);return {'ok':True}
        try:
            with patch.object(ProfileStore,'_connect',connect),patch('probe.probe_snapshot',side_effect=slow):
                started=self.start(jobs,protocol='chat_completions',base_url='https://example.invalid/v1',provider_id=self.provider['id'],model=models[0],models=models,key='')
                self.assertTrue(entered.wait(3))
                self.assertLess(len(opens),20)
                self.assertTrue(jobs.cancel(started['job_id'])['ok'])
                done=self.finish(jobs,started)
                self.assertEqual(done['state'],'cancelled')
        finally:release.set()

    def test_atomic_commit_status_does_not_block_and_cancel_is_honest(self):
        jobs=self.jobs();entered=threading.Event();release=threading.Event()
        publish=jobs.publish_fn
        def slow(store,manager):entered.set();release.wait(5);return publish(store,manager)
        jobs.publish_fn=slow
        try:
            with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
                started=self.start(jobs);self.authorize(jobs,started)
                self.assertTrue(entered.wait(3))
                now=time.monotonic();status=jobs.status(started['job_id'])
                self.assertLess(time.monotonic()-now,1)
                self.assertEqual(status['state'],'committing',status)
                now=time.monotonic()
                self.assertEqual(self.start(jobs,client_job_id=started['job_id'])['state'],'committing')
                self.assertLess(time.monotonic()-now,1)
                self.assertEqual(jobs.cancel(started['job_id'])['code'],'commit_in_progress')
                release.set();done=self.finish(jobs,started)
                self.assertEqual(done['state'],'succeeded',done)
        finally:release.set()

    def test_process_crash_after_atomic_commit_preserves_saved_receipt(self):
        root=self.root/'committed-crash';ident=str(uuid.uuid4())
        code="""import sys,time,os,probe
from pathlib import Path
from storage import ProfileStore
from save_jobs import SaveJobs
probe.probe_jev=lambda *a:{'ok':True}
class CrashJobs(SaveJobs):
 def _record(self,job,db=None):
  if job['state']=='succeeded':os._exit(0)
  return super()._record(job,db)
root=Path(sys.argv[1]);j=CrashJobs(store_factory=lambda:ProfileStore(root))
r=j.start(client_job_id=sys.argv[2],name='Crash',protocol='system_one',base_url='https://fixture.invalid/systemone',model='jev-a',models=['jev-a','jev-b'],key='synthetic-key')
while j.status(r['job_id'])['state']!='ready':time.sleep(.01)
j.commit(r['job_id'])
time.sleep(15)
raise RuntimeError('worker did not exit')
"""
        subprocess.run([sys.executable,'-B','-c',code,str(root),ident],cwd=Path(__file__).parent,check=True,timeout=20)
        recovered=SaveJobs(store_factory=lambda:ProfileStore(root)).status(ident)
        self.assertEqual(recovered['state'],'committed_cleanup_required',recovered)
        self.assertTrue(recovered['result']['activation']['ok'])
        self.assertEqual(len(ProfileStore(root).list()),1)

    def test_uuid_registration_is_atomic_across_managers(self):
        ident=str(uuid.uuid4());one=self.jobs();two=self.jobs()
        entered=threading.Event();release=threading.Event();calls=[]
        def slow(*args):
            calls.append(1);entered.set();release.wait(5)
            raise ValueError('synthetic rejection')
        one.candidate_fn=two.candidate_fn=slow
        replies=[]
        workers=[threading.Thread(target=lambda j=j:replies.append(self.start(j,client_job_id=ident))) for j in (one,two)]
        try:
            for t in workers:t.start()
            for t in workers:t.join(3);self.assertFalse(t.is_alive())
            self.assertTrue(entered.wait(2));self.assertEqual(len(calls),1)
            self.assertEqual({r['job_id'] for r in replies},{ident})
        finally:
            release.set()
            for j in (one,two):
                if ident in j.jobs:self.finish(j,{'job_id':ident})

    def test_cleanup_failure_is_committed_and_durable_not_unsaved(self):
        jobs=self.jobs()
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False),patch('engine_control.cleanup_unpublished_providers',side_effect=OSError('synthetic cleanup error')):
            done=self.finish(jobs,self.start(jobs))
        self.assertEqual(done['state'],'committed_cleanup_required',done)
        self.assertTrue(done['cleanup_pending'])
        self.assertTrue(done['result']['activation']['ok'])
        self.assertTrue(any(p['id']==done['result']['profile']['id'] for p in self.store.list()))
        self.assertEqual(self.jobs().status(done['job_id'])['state'],'committed_cleanup_required')

    def test_many_models_have_real_bindings_and_complete_activation_result(self):
        jobs=self.jobs();models=['fixture-model'+str(i) for i in range(25)]+['fixture-beta']
        with patch('probe.probe_snapshot',return_value={'ok':True}) as calls,patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
            started=self.start(jobs,model=models[0],models=models)
            done=self.finish(jobs,started)
        self.assertEqual(done['state'],'succeeded',done)
        result=done['result'];self.assertTrue(result['activation']['ok'])
        aliases=result['activation']['aliases'];self.assertEqual(len(aliases),26)
        self.assertTrue(any(a.endswith('/fixture-beta') for a in aliases))
        self.assertEqual(calls.call_count,26)
        self.assertEqual(done['tested'],26)
        for alias in aliases:
            self.assertTrue(named_route_ready(self.store,self.home,alias),alias)
        profile=next(p for p in self.store.list() if p['id']==result['profile']['id'])
        self.assertEqual(profile['models'],models)
        self.assertNotIn('synthetic-job-secret',json.dumps(done))

    def test_contract_accepts_all_5000_discovered_models(self):
        models=['model'+str(i) for i in range(5000)]
        self.assertEqual(validate_models(models[0],models,'responses'),models)
        with self.assertRaises(ValueError):validate_models(models[0],models+['extra'],'responses')

    def test_failed_later_probe_never_saves_any_model(self):
        jobs=self.jobs();before=self.store.list()
        with patch('probe.probe_snapshot',side_effect=lambda route,*a:{'ok':route['model']!='fixture-beta','code':'upstream_rejected'}),patch('probe.discover_capabilities',return_value={}):
            done=self.finish(jobs,self.start(jobs))
        self.assertEqual(done['state'],'failed')
        self.assertEqual(done['result']['failed_model'],'fixture-beta')
        self.assertEqual(self.store.list(),before)
        self.assertEqual(self.keys,{})

    def test_cancelled_read_cannot_resume_a_late_save(self):
        jobs=self.jobs();entered=threading.Event();release=threading.Event();before=self.store.list()
        def slow(*args):entered.set();release.wait(5);return {'ok':True}
        try:
            with patch('probe.probe_snapshot',side_effect=slow):
                started=self.start(jobs);self.assertTrue(entered.wait(2))
                self.assertTrue(jobs.cancel(started['job_id'])['ok'])
                done=self.finish(jobs,started)
            self.assertEqual(done['state'],'cancelled',done)
            release.set();time.sleep(.05)
            self.assertEqual(self.store.list(),before)
        finally:release.set()

    def test_cancel_during_engine_staging_leaves_old_route_and_no_half_publication(self):
        jobs=self.jobs();entered=threading.Event();release=threading.Event()
        replace=self.manager.replace_provider;calls=0
        def pause(name,value):
            nonlocal calls
            calls+=1
            result=replace(name,value)
            if calls==1:entered.set();release.wait(5)
            return result
        before=self.store.list()
        try:
            with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
                self.manager.replace_provider=pause
                started=self.start(jobs);self.authorize(jobs,started);self.assertTrue(entered.wait(2))
                self.assertEqual(self.store.list(),before)
                self.assertTrue(jobs.cancel(started['job_id'])['ok']);release.set()
                done=self.finish(jobs,started)
            self.assertEqual(done['state'],'cancelled',done)
            self.assertEqual(self.store.list(),before)
            self.assertEqual(self.keys,{})
            self.assertEqual(set(self.config['providers']),{'openai'})
        finally:release.set()

    def test_jev_saved_set_selects_current_and_updates_same_connection(self):
        from jev_hub import JevCredentials
        jobs=self.jobs();models=['jev-one','jev-two','jev-three']
        with patch('probe.probe_jev',return_value={'ok':True}) as calls:
            done=self.finish(jobs,self.start(jobs,protocol='system_one',model=models[0],models=models))
        self.assertEqual(done['state'],'succeeded',done)
        ident=done['result']['profile']['id'];self.assertEqual(self.store.get_jev_profile_id(),ident)
        credentials=JevCredentials(self.store)
        self.assertEqual(credentials.read_settings()['model'],models[0])
        with patch('probe.probe_jev',return_value={'ok':True}):
            updated=self.finish(jobs,self.start(jobs,protocol='system_one',provider_id=ident,model=models[1],models=[models[1],models[0],models[2]]))
        self.assertEqual(updated['state'],'succeeded',updated)
        self.assertEqual(updated['result']['profile']['id'],ident)
        self.assertEqual(credentials.read_settings()['model'],models[1])
        self.assertEqual(len([p for p in self.store.list() if p['name']=='Job']),1)

    def test_alias_length_is_rejected_before_any_paid_probe(self):
        jobs=self.jobs()
        with patch('probe.probe_snapshot') as probe_call:
            done=self.finish(jobs,self.start(jobs,model='m'*160,models=['m'*160,'native-alpha']))
            self.assertEqual(done['state'],'failed')
            self.assertEqual(done['result']['code'],'model_alias_invalid')
            self.assertEqual(done['result']['failed_model'],'m'*160)
        probe_call.assert_not_called()

    def test_disabled_connections_reenable_all_models_for_each_protocol(self):
        from engine_control import disable_provider
        jobs=self.jobs()
        for protocol in ('responses','system_one','codex_subscription'):
            models=['fixture-model-a','fixture-model-b','fixture-model-c','fixture-model-d']
            p=self.store.save('Reenable',protocol,'https://fixture.invalid/v1',models[0],
                              None if protocol=='codex_subscription' else 'synthetic-key',models=models)
            self.assertTrue(disable_provider(self.store,p['id'],self.manager)['ok'])
            self.assertTrue(self.store.is_disabled(p['id']))
            def official_refresh(root,store=None):
                self.assertFalse(store.is_disabled(p['id']))
                self.assertEqual(store.model_ids(p['id']),models)
            with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.probe_jev',return_value={'ok':True}), \
                 patch('probe.probe_official',return_value={'ok':True}),patch('probe.official_capabilities',return_value={}), \
                 patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False), \
                 patch('catalog.refresh',side_effect=official_refresh):
                done=self.finish(jobs,self.start(jobs,protocol=protocol,model=models[0],models=models,provider_id=p['id'],key=''))
            self.assertEqual(done['state'],'succeeded',done)
            self.assertFalse(self.store.is_disabled(p['id']))
            self.assertTrue(done['result']['activation']['ok'])
            if protocol=='system_one':self.assertEqual(self.store.get_jev_profile_id(),p['id'])
            if protocol=='responses':
                self.assertEqual(len(done['result']['activation']['aliases']),4)
                self.assertTrue(all(named_route_ready(self.store,self.home,a) for a in done['result']['activation']['aliases']))

    def test_generated_routes_survive_store_reopen_then_disable_reenable_delete(self):
        from engine_control import disable_provider, delete_provider
        jobs=self.jobs();models=['native-alpha','vendor/fixture-beta']
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs,models=models))
            self.assertEqual(done['state'],'succeeded',done)
            ident=done['result']['profile']['id'];aliases=done['result']['activation']['aliases']
            reopened=ProfileStore(self.store.root)
            self.assertTrue(all(named_route_ready(reopened,self.home,a) for a in aliases))
            self.assertTrue(all(not a.startswith(reopened.get_engine_binding(a)['account'].split('/')[0]+'/') for a in aliases))
            self.assertTrue(disable_provider(reopened,ident,self.manager)['ok'])
            self.assertTrue(all(not named_route_ready(reopened,self.home,a) for a in aliases))
            again=self.finish(jobs,self.start(jobs,provider_id=ident,models=models,key=''))
            self.assertEqual(again['state'],'succeeded',again)
            self.assertTrue(delete_provider(ProfileStore(self.store.root),ident,True,self.manager,self.keyring)['ok'])
        self.assertFalse(any(p['id']==ident for p in self.store.list()))
        self.assertEqual(set(self.config['providers']),{'openai'})
        self.assertEqual(self.keys,{})

    def test_success_is_exposed_only_after_activation_lock_and_cleanup_release(self):
        from engine_control import activation_lock,disable_provider
        jobs=self.jobs();entered=threading.Event();release=threading.Event()
        def slow_cleanup(*args):entered.set();release.wait(5);return 0
        try:
            with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}), \
                 patch('engine_control._requires_private_network',return_value=False),patch('engine_control.cleanup_unpublished_providers',side_effect=slow_cleanup):
                started=self.start(jobs);self.authorize(jobs,started);self.assertTrue(entered.wait(2))
                during=jobs.status(started['job_id'])
                self.assertEqual(during['state'],'finalizing',during)
                self.assertFalse(jobs.cancel(started['job_id'])['ok'])
                # A new manager can distinguish a committed job from a precommit interruption.
                recovered=self.jobs().status(started['job_id'])
                self.assertEqual(recovered['state'],'committed_cleanup_required')
                release.set();done=self.finish(jobs,started)
            self.assertEqual(done['state'],'succeeded',done)
            with activation_lock(self.store.root):
                self.assertTrue(disable_provider(self.store,done['result']['profile']['id'],self.manager)['ok'])
        finally:release.set()

    def test_removed_hub_alias_is_not_a_guard_candidate_even_with_stale_catalog(self):
        from engine_control import delete_provider
        from importlib.util import spec_from_file_location,module_from_spec
        jobs=self.jobs()
        with patch('probe.probe_snapshot',return_value={'ok':True}),patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs))
        ident=done['result']['profile']['id'];alias=done['result']['activation']['aliases'][0]
        local=self.root/'isolated-local';target=local/'CodexProviderHub';target.mkdir(parents=True)
        # Use the actual saved DB and catalog via a synthetic LOCALAPPDATA root.
        import shutil
        home=self.root/'catalog-home';home.mkdir()
        (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':'native-beta','visibility':'list'}]}),encoding='utf-8')
        catalog=home/'catalog.json'
        official={'slug':'native-beta','visibility':'list','supported_reasoning_levels':[{'effort':'high'}]}
        catalog.write_text(json.dumps({'models':[official,{'slug':alias,'visibility':'list','supported_reasoning_levels':[{'effort':'high'}]}]}))
        (home/'config.toml').write_text('model_catalog_json = "'+catalog.as_posix()+'"')
        script=Path(__file__).parent/'scripts/read-model-catalog.py'
        spec=spec_from_file_location('guard_catalog_fixture',script);module=module_from_spec(spec);spec.loader.exec_module(module)
        shutil.copy2(self.store.path,target/'profiles.sqlite')
        with patch.dict(os.environ,{'LOCALAPPDATA':str(local)}):
            before_view=module.read_catalog(home)
            self.assertIn(alias,before_view['models'])
            self.assertTrue(delete_provider(self.store,ident,True,self.manager,self.keyring)['ok'])
            shutil.copy2(self.store.path,target/'profiles.sqlite')
            after_view=module.read_catalog(home)
            models=after_view['models']
            self.assertNotIn(alias,models)
            self.assertIn('native-beta',models)
            # Feed actual Python reader output into the actual model-policy guard.
            code="const p=require('./lib/model-policy.cjs');const v=JSON.parse(process.argv[1]);process.stdout.write(JSON.stringify(p.validateSpawn({model:process.argv[2],reasoning_effort:'high'},{schemaVersion:2,allowedFamilies:[process.argv[2]]},v)));"
            for view, expected in ((before_view,'MODEL_SELECTION_VALID'),(after_view,'MODEL_NOT_ALLOWED')):
                result=subprocess.run(['node','-e',code,json.dumps(view),alias],cwd=Path(__file__).parent,capture_output=True,text=True,encoding='utf-8',check=True)
                self.assertEqual(json.loads(result.stdout)['code'],expected)

    def test_unknown_effort_receipt_is_committed_without_claiming_high_or_max(self):
        jobs=self.jobs()
        with patch('probe.probe_snapshot',return_value={'ok':True}) as calls,patch('probe.discover_capabilities',return_value={}),patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs))
        self.assertEqual(done['state'],'succeeded',done)
        alias=alias_for(self.store,done['result']['profile']['id'])
        profile,_=self.store.resolve_route(alias)
        self.assertEqual(self.store.confirmed_efforts(profile),set())
        self.assertEqual(next(c.args[2] for c in calls.call_args_list if c.args[0]['model']=='fixture-beta'),None)
        receipt=next(r for r in done['result']['probe_receipts'] if r['model']=='fixture-beta')
        self.assertEqual((receipt['alias'],receipt['revision'],receipt['effort']),(alias,profile['_revision'],None))
        with self.store.connection() as db:
            rows=db.execute('SELECT model,effort,revision FROM model_probe_receipts WHERE provider_id=?',(profile['id'],)).fetchall()
        self.assertEqual(len(rows),2)
        self.assertNotIn('max',{row[1] for row in rows})

    def test_configured_efforts_require_explicit_confirmation(self):
        jobs=self.jobs()
        settings={'fixture-beta':{'reasoning_levels':['high','max'],'default_effort':'high'}}
        with patch('probe.probe_snapshot',return_value={'ok':True}) as calls, \
             patch('probe.discover_capabilities',return_value={}), \
             patch('engine_control._requires_private_network',return_value=False):
            done=self.finish(jobs,self.start(jobs,per_model_settings=settings))
        self.assertEqual(done['state'],'succeeded',done)
        alias=alias_for(self.store,done['result']['profile']['id'])
        profile,_=self.store.resolve_route(alias)
        self.assertEqual(self.store.confirmed_efforts(profile),set())
        self.assertEqual(next(c.args[2] for c in calls.call_args_list if c.args[0]['model']=='fixture-beta'),None)
        receipt=next(r for r in done['result']['probe_receipts'] if r['model']=='fixture-beta')
        self.assertEqual((receipt['alias'],receipt['revision'],receipt['effort']),
                         (alias,profile['_revision'],None))
        # A configured default is not probe evidence. Record only the effort
        # actually confirmed, for this model and content revision.
        self.store.confirm_effort(alias,profile['_revision'],'high')
        self.assertEqual(self.store.confirmed_efforts(profile),{'high'})
        other_alias=alias_for(self.store,profile['id'],'native-alpha')
        other,_=self.store.resolve_route(other_alias)
        self.assertEqual(self.store.confirmed_efforts(other),set())
        self.assertNotIn('max',self.store.confirmed_efforts(profile))
        with self.assertRaises(ValueError):
            self.store.confirm_effort(alias,'stale-revision','max')

    def test_process_termination_does_not_resume_saving(self):
        root=self.root/'terminated'
        code="""import sys,time,probe; from pathlib import Path; from storage import ProfileStore; from save_jobs import SaveJobs
probe.probe_jev=lambda *a:(time.sleep(60) or {'ok':True})
root=Path(sys.argv[1]);j=SaveJobs(store_factory=lambda:ProfileStore(root))
r=j.start(client_job_id='1779aabc-5c25-471e-983d-4cb0929a236e',name='Interrupted',protocol='system_one',base_url='https://fixture.invalid/systemone',model='jev-a',models=['jev-a','jev-b'],key='synthetic-key')
print(r['job_id'],flush=True)
time.sleep(60)
"""
        process=subprocess.Popen([sys._base_executable,'-B','-c',code,str(root)],env=dict(os.environ,PYTHONPATH=os.pathsep.join(sys.path)),cwd=Path(__file__).parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            ident=process.stdout.readline().strip();self.assertTrue(ident)
        finally:
            process.terminate();process.wait(timeout=5)
            process.stdout.close();process.stderr.close()
        jobs=SaveJobs(store_factory=lambda:ProfileStore(root))
        self.assertIn(jobs.status(ident)['state'],{'detached','interrupted'})
        self.assertEqual(ProfileStore(root).list(),[])


if __name__=='__main__':unittest.main()
