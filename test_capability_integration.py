import os
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

# Import-time defaults must also stay inside a disposable environment.
with tempfile.TemporaryDirectory(prefix='hub-neutral-import-') as import_root:
    with patch.dict(os.environ, {'LOCALAPPDATA':import_root, 'CODEX_HOME':str(Path(import_root)/'codex'), 'HF_HUB_OFFLINE':'1'}):
        import mcp_server
from storage import ProfileStore, _dpapi
from ocx_service import ENGINE_VERSION


class CapabilityIntegrationTests(unittest.TestCase):
    def setUp(self):
        offline = patch.dict(os.environ, {'HF_HUB_OFFLINE':'1'})
        offline.start()
        self.addCleanup(offline.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from test_neutral_python_compat import isolated_native_catalog
        isolated_native_catalog(self, self.root)
        self.store = ProfileStore(self.root)
        profile = self.store.save('样例线路', 'gemini_native', 'https://example.test',
                                  'fixture-vision', 'synthetic-key')
        self.ident = profile['id']
        self.alias = 'hub-' + self.ident.replace('-', '') + '/fixture-vision'
        self.model = 'fixture-vision'
        resolved, _ = self.store.resolve_profile(self.ident)
        account = 'hub-' + self.ident.replace('-', '') + '/22222222-2222-4222-8222-222222222222'
        self.store.bind_engine_route(self.alias, self.ident, resolved['_revision'], account)

    def test_recommendation_runs_one_sample_then_reuses_it(self):
        selected = {'ok': True, 'model': self.alias, 'effort': 'high', 'dispatch_status': 'pending_dispatch'}
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', side_effect=lambda *args, **kwargs: selected.copy()), \
             patch.object(mcp_server, 'probe_capability', return_value={'status':'passed'}) as probe:
            first = mcp_server.recommend_subagent_model('bounded', purpose='tool')
            second = mcp_server.recommend_subagent_model('bounded', purpose='tool')
        self.assertEqual(first['purpose_status'], 'sample_passed')
        self.assertFalse(first['capability_sample']['cached'])
        self.assertTrue(second['capability_sample']['cached'])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(first['dispatch_status'], 'pending_dispatch')
        self.assertNotIn('synthetic-key', str(first))

    def test_recommendation_uses_loopback_gateway_then_reuses_saved_result(self):
        received = []
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                body = b'{"capability_binding_guard":true,"capability_deadline_guard":true}'
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path, self.headers.get('X-Provider-Hub-Token'), body))
                message = {'type':'message', 'status':'completed', 'content':[
                    {'type':'output_text','text':'{"left":"red","right":"blue"}'}]}
                event = {'type':'response.completed','response':{'status':'completed','output':[message]}}
                wire = ('data: ' + json.dumps(event) + '\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(wire)))
                self.end_headers()
                self.wfile.write(wire)
        gateway = ThreadingHTTPServer(('127.0.0.1',0), Gateway)
        worker = threading.Thread(target=gateway.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(gateway.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(gateway.shutdown)
        (self.root/'gateway-state.json').write_text(json.dumps({'engine':ENGINE_VERSION,
            'port':gateway.server_port,'inner_port':gateway.server_port}), encoding='utf-8')
        (self.root/'gateway-capability.bin').write_bytes(_dpapi(b'synthetic-local-token'))
        selected={'ok':True,'model':self.alias,'effort':'high','dispatch_status':'pending_dispatch'}
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', side_effect=lambda *args, **kwargs: selected.copy()):
            first = mcp_server.recommend_subagent_model('bounded', purpose='image')
            second = mcp_server.recommend_subagent_model('bounded', purpose='image')
        self.assertEqual(first['purpose_status'], 'sample_passed')
        self.assertTrue(second['capability_sample']['cached'])
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0:2], ('/v1/responses', 'synthetic-local-token'))
        self.assertEqual(received[0][2]['model'], self.alias)
        self.assertEqual(received[0][2]['reasoning']['effort'], 'high')
        self.assertNotIn('synthetic-local-token', str(first))

    def test_explicit_model_is_not_replaced_after_sample_failure(self):
        selected = {'ok':True, 'model':self.alias, 'effort':'high', 'dispatch_status':'pending_dispatch'}
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', return_value=selected.copy()) as schedule, \
             patch.object(mcp_server, 'probe_capability', return_value={'status':'failed'}):
            result = mcp_server.recommend_subagent_model('bounded', self.alias, 'high', 'image')
        self.assertEqual(result['model'], self.alias)
        self.assertEqual(result['purpose_status'], 'sample_failed')
        self.assertEqual(schedule.call_count, 1)

    def test_overview_evidence_is_sanitized_and_channel_specific(self):
        self.store.record_capability_evidence(self.ident, self.model, 'video', 'passed',
                                              channel='gemini_media')
        self.store.record_capability_evidence(self.ident, self.model, 'tool', 'error', 'high',
                                              channel='subagent_gateway', error_code='rate_limit')
        result = mcp_server._route_evidence(self.store)[self.alias]
        self.assertEqual({(x['purpose'], x['channel'], x['status']) for x in result},
                         {('video','gemini_media','passed'), ('tool','subagent_gateway','error')})
        self.assertTrue(all('identity' not in x and 'provider_id' not in x for x in result))
        self.assertTrue(all(isinstance(x['tested_at'], str) for x in result))
        self.assertNotIn('synthetic-key', str(result))

    def test_media_recommendation_does_not_claim_subagent_media(self):
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store):
            result = mcp_server.recommend_subagent_model('boundary', purpose='video')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'MEDIA_TOOL_REQUIRED')
        self.assertEqual(result['tool'], 'analyze_gemini_media')
        self.assertEqual(result['connection'], '样例线路')

    def test_media_snapshot_blocks_interleaved_aba_edit(self):
        started, finished = threading.Event(), threading.Event()
        errors = []
        def writer():
            started.wait(5)
            try:
                other = ProfileStore(self.root)
                other.save('样例线路', 'gemini_native', 'https://example.test/other', self.model, None, self.ident)
                other.save('样例线路', 'gemini_native', 'https://example.test', self.model, None, self.ident)
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                finished.set()
        thread = threading.Thread(target=writer)
        thread.start()
        read_profile = self.store._read_profile
        def interleave(db, *args):
            value = read_profile(db, *args)
            started.set()
            self.assertFalse(finished.wait(0.1), 'edit must not commit inside the snapshot')
            return value
        try:
            with patch.object(self.store, '_read_profile', side_effect=interleave):
                profile, secret, identity = self.store.resolve_media_capability(self.ident, self.model)
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(profile['base_url'], identity['base_url'])
        self.assertEqual(profile['base_url'], 'https://example.test')
        self.assertEqual(secret, 'synthetic-key')

    def test_media_first_use_and_cached_reuse(self):
        class ImmediateThread:
            def __init__(self, target, **kwargs): self.target = target
            def start(self): self.target()
        sample = self.root / 'video.mp4'
        sample.write_bytes(b'synthetic-video')
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server.threading, 'Thread', ImmediateThread), \
             patch.object(mcp_server, 'probe_gemini_snapshot', return_value={'status':'passed'}) as probe, \
             patch.object(mcp_server, 'inspect_media', side_effect=lambda *args: {'ok':True,'answer':'sample answer'}):
            first = mcp_server.analyze_gemini_media(str(sample), '描述画面')
            second = mcp_server.analyze_gemini_media(str(sample), '描述画面')
        self.assertEqual(first['capability_sample']['status'], 'testing')
        self.assertEqual(second['capability_sample']['status'], 'passed')
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(self.store.capability_evidence(self.ident, self.model)[0]['channel'], 'gemini_media')

    def test_invalid_media_never_starts_sample(self):
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_start_media_sample') as sample, \
             patch.object(mcp_server, 'inspect_media', return_value={'ok':False,'code':'MEDIA_UNREADABLE'}):
            mcp_server.analyze_gemini_media('relative.mp4', '看视频')
        sample.assert_not_called()

    def test_changed_credentials_before_background_sample_never_reach_new_line(self):
        pending = []
        class DeferredThread:
            def __init__(self, target, **kwargs): pending.append(target)
            def start(self): pass
        sample = self.root / 'video.mp4'
        sample.write_bytes(b'synthetic-video')
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server.threading, 'Thread', DeferredThread), \
             patch.object(mcp_server, 'probe_gemini_snapshot') as probe, \
             patch.object(mcp_server, 'inspect_media', return_value={'ok':True}):
            result = mcp_server.analyze_gemini_media(str(sample), '描述画面')
            self.assertEqual(result['capability_sample']['status'], 'testing')
            self.store.save('样例线路', 'gemini_native', 'https://changed.example.test',
                            self.model, 'new-synthetic-key', self.ident)
            pending[0]()
        probe.assert_not_called()
        self.assertEqual(self.store.capability_evidence(self.ident, self.model), [])

    def test_failed_candidate_causes_backup_to_be_sampled(self):
        other = self.store.save('备用', 'chat_completions', 'https://backup.example.test/v1',
                                'other-model', 'synthetic-key')
        other_alias = 'hub-' + other['id'].replace('-', '') + '/other-model'
        resolved, _ = self.store.resolve_profile(other['id'])
        account = 'hub-' + other['id'].replace('-', '') + '/33333333-3333-4333-8333-333333333333'
        self.store.bind_engine_route(other_alias, other['id'], resolved['_revision'], account)
        candidates = [{'ok':True, 'model':self.alias, 'effort':'high'},
                      {'ok':True, 'model':other_alias, 'effort':'high'}]
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', side_effect=candidates) as schedule, \
             patch.object(mcp_server, 'probe_capability', side_effect=[{'status':'failed'}, {'status':'passed'}]) as probe:
            result = mcp_server.recommend_subagent_model('bounded', purpose='tool')
        self.assertTrue(result['ok'])
        self.assertEqual(result['model'], other_alias)
        self.assertEqual(result['purpose_status'], 'sample_passed')
        self.assertEqual((schedule.call_count, probe.call_count), (2, 2))
        self.assertEqual(result['previous_samples'][0]['status'], 'failed')

    def test_unverified_subscription_backup_is_explicitly_marked_as_not_tested(self):
        candidates = [{'ok':True, 'model':self.alias, 'effort':'high'},
                      {'ok':True, 'model':'native-alpha', 'effort':'high'}]
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', side_effect=candidates), \
             patch.object(mcp_server, 'probe_capability', return_value={'status':'failed'}) as probe:
            result = mcp_server.recommend_subagent_model('bounded', purpose='image')
        self.assertTrue(result['ok'])
        self.assertEqual((result['model'], result['purpose_status']), ('native-alpha', 'not_tested'))
        self.assertEqual(result['previous_samples'][0]['status'], 'failed')
        self.assertEqual(probe.call_count, 1)
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', return_value={'ok':True,'model':'native-alpha','effort':'high'}), \
             patch.object(mcp_server, 'probe_capability') as first_probe:
            first = mcp_server.recommend_subagent_model('bounded', purpose='image')
        self.assertEqual((first['ok'], first['purpose_status']), (result['ok'], result['purpose_status']))
        first_probe.assert_not_called()

    def test_transient_route_change_is_excluded_only_for_the_current_recommendation(self):
        other = self.store.save('备用', 'chat_completions', 'https://backup.example.test/v1',
                                'other-model', 'synthetic-key')
        other_alias = 'hub-' + other['id'].replace('-', '') + '/other-model'
        resolved, _ = self.store.resolve_profile(other['id'])
        account = 'hub-' + other['id'].replace('-', '') + '/33333333-3333-4333-8333-333333333333'
        self.store.bind_engine_route(other_alias, other['id'], resolved['_revision'], account)
        requests = []
        def schedule(_action, payload, **kwargs):
            requests.append(dict(payload))
            alias = other_alias if payload.get('excluded') else self.alias
            return {'ok':True, 'model':alias, 'effort':'high'}
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', side_effect=schedule), \
             patch.object(mcp_server, 'ensure_capability', side_effect=[
                 {'status':'error','reason':'route_changed'}, {'status':'passed'}]):
            result = mcp_server.recommend_subagent_model('bounded', purpose='tool')
        self.assertEqual(result['model'], other_alias)
        self.assertEqual(requests[1]['excluded'], [{'model':self.alias,'effort':'high'}])
        self.assertEqual(self.store.capability_evidence(self.ident, self.model), [])

    def test_budget_expiry_stops_fallback_requests(self):
        clock = [100.0]
        selected = {'ok':True, 'model':self.alias, 'effort':'high'}
        def probe(*args, **kwargs):
            clock[0] += mcp_server.SAMPLE_RECOMMEND_SECONDS
            return {'status':'error', 'reason':'timeout'}
        with patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', return_value=selected.copy()) as schedule, \
             patch.object(mcp_server, 'probe_capability', side_effect=probe) as check, \
             patch.object(mcp_server.time, 'monotonic', side_effect=lambda:clock[0]):
            result = mcp_server.recommend_subagent_model('bounded', purpose='tool')
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'PURPOSE_SAMPLE_PENDING')
        self.assertEqual((schedule.call_count, check.call_count), (1, 1))


if __name__ == '__main__':
    unittest.main()
