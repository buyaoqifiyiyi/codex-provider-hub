"""All network endpoints and credentials below are isolated synthetic fixtures."""
import base64
import io
import os
import json
from pathlib import Path
import struct
import threading
import tempfile
import time
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import wave
import zlib

import capability_probes as probes


class FakeStore:
    root = Path('/synthetic-unused')

    def __init__(self, protocol='gemini_native'):
        self.profile = {'id': 'synthetic-id', 'model': 'synthetic-model', 'models': ['synthetic-model', 'other-model'],
                        'protocol': protocol, 'base_url': 'https://synthetic.invalid',
                        '_revision': 'synthetic-revision-A', 'account': 'synthetic/account-A'}

    def get_engine_binding(self, alias):
        return self.profile.copy() if alias == 'sample-alias' else None

    get_route = get_engine_binding

    def is_disabled(self, ident):
        return False

    def resolve_route(self, alias):
        return self.profile.copy(), 'synthetic-provider-secret'

    def list(self):
        return [self.profile.copy()]

    resolve_profile = resolve_route


def message(text):
    return {'type': 'message', 'role': 'assistant',
            'content': [{'type': 'output_text', 'text': text}]}


def sse(output):
    return ('event: response.completed\ndata: ' + json.dumps({
        'type': 'response.completed', 'response': {'status': 'completed', 'output': output}
    }) + '\n\ndata: [DONE]\n\n').encode()


class Response(io.BytesIO):
    headers = {'Content-Type': 'text/event-stream'}
    status = 200


class CapabilityProbeTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.calls = []
        self.state = patch('ocx_service._state', return_value={'port': 12345})
        self.token = patch('service._read_capability', return_value='synthetic-gateway-secret')
        self.state.start()
        self.token.start()
        self.addCleanup(self.state.stop)
        self.addCleanup(self.token.stop)

    def run_probe(self, purpose, transport, **kwargs):
        if transport is not None:
            underlying = transport
            def transport(request, timeout):
                if request.get_method() == 'GET':
                    return Response(b'{"capability_binding_guard":true,"capability_deadline_guard":true}')
                return underlying(request, timeout=timeout)
        result = probes.probe_capability(self.store, 'sample-alias', purpose,
                                         transport=transport, **kwargs)
        self.assertIn(result['reason'], probes.REASONS)
        self.assertNotIn('synthetic-provider-secret', str(result))
        self.assertNotIn('synthetic-gateway-secret', str(result))
        self.assertNotIn('answer', result)
        return result

    def gateway(self, request, timeout):
        self.assertEqual(request.full_url, 'http://127.0.0.1:12345/v1/responses')
        self.assertEqual(request.get_header('X-provider-hub-token'), 'synthetic-gateway-secret')
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, probes.REQUEST_TIMEOUT)
        body = json.loads(request.data)
        self.calls.append(body)
        self.assertEqual(body['model'], 'sample-alias')
        self.assertIs(body['stream'], True)
        self.assertLessEqual(body['max_output_tokens'], 512)
        if len(self.calls) == 1:
            return Response(sse([{'type': 'function_call', 'name': 'capability_lookup',
                'call_id': 'synthetic-call', 'arguments': '{"key":"sample"}'}]))
        tool_result = body['input'][-1]
        self.assertEqual(tool_result['type'], 'function_call_output')
        self.assertEqual(tool_result['call_id'], 'synthetic-call')
        self.assertEqual(body['input'][-2]['type'], 'function_call')
        return Response(sse([message(tool_result['output'])]))

    def test_tool_roundtrip_requires_result_and_same_effort(self):
        result = self.run_probe('tool', self.gateway, effort='high')
        self.assertEqual((result['status'], result['channel'], result['effort']),
                         ('passed', 'subagent_gateway', 'high'))
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(b['reasoning'] == {'effort': 'high'} for b in self.calls))

    def test_tool_no_call_and_wrong_receipt_fail_semantically(self):
        result = self.run_probe('tool', lambda *a, **k: Response(sse([message('I will not call')])) )
        self.assertEqual(result['status'], 'failed')
        def wrong(request, timeout):
            if not self.calls:
                return self.gateway(request, timeout)
            return Response(sse([message('{"receipt":"guessed"}')]))
        self.assertEqual(self.run_probe('tool', wrong)['status'], 'failed')

    def test_malformed_tool_call_is_error(self):
        call = {'type': 'function_call', 'name': 'capability_lookup',
                'call_id': 'c', 'arguments': 'not json'}
        self.assertEqual(self.run_probe('tool', lambda *a, **k: Response(sse([call])))['status'], 'error')

    def test_image_bytes_and_exact_colour_check(self):
        def image(request, timeout):
            body = json.loads(request.data)
            url = body['input'][0]['content'][1]['image_url']
            self.assertEqual(base64.b64decode(url.split(',')[1]), probes._asset('two-colour.png'))
            return Response(sse([message('{"left":"red","right":"blue"}')]))
        self.assertEqual(self.run_probe('image', image)['status'], 'passed')
        for answer, status in [('{"left":"blue","right":"red"}', 'failed'),
                               ('not json', 'error'), ('{"left":true,"right":false}', 'error')]:
            self.assertEqual(self.run_probe('image', lambda *a, **k: Response(sse([message(answer)])))['status'], status)

    def test_http_200_and_partial_stream_never_pass(self):
        for raw in [b'{}', b'data: [DONE]\n\n', b'data: {bad}\n\n',
                    b'data: {"type":"response.output_text.delta","delta":"red blue"}\n\n',
                    b'data: {"type":"response.incomplete"}\n\n']:
            self.assertEqual(self.run_probe('image', lambda *a, **k: Response(raw))['status'], 'error')

    def test_bad_content_type_truncated_terminal_and_incomplete_item(self):
        raw = sse([message('{"left":"red","right":"blue"}')])
        def wrong_type(*a, **k):
            response = Response(raw)
            response.headers = {'Content-Type': 'application/json'}
            return response
        self.assertEqual(self.run_probe('image', wrong_type)['reason'], 'format')
        self.assertEqual(self.run_probe('image', lambda *a, **k: Response(raw.rstrip()))['reason'], 'format')
        item = message('{"left":"red","right":"blue"}')
        item['status'] = 'incomplete'
        self.assertEqual(self.run_probe('image', lambda *a, **k: Response(sse([item])))['reason'], 'incomplete')

    def test_tool_never_retries_a_third_call(self):
        def loop(request, timeout):
            self.calls.append(json.loads(request.data))
            return Response(sse([{'type': 'function_call', 'name': 'capability_lookup',
                'call_id': 'c', 'arguments': '{"key":"sample"}'}]))
        self.assertEqual(self.run_probe('tool', loop)['status'], 'failed')
        self.assertEqual(len(self.calls), 2)

    def test_media_bad_json_and_wrong_semantics_are_distinct(self):
        for answer, status in [('not json', 'error'), ('{"number":418,"code":"海风"}', 'failed')]:
            r = self.run_probe('speech', None, media_inspector=lambda *a, **k: {'ok': True, 'answer': answer})
            self.assertEqual(r['status'], status)
        r = self.run_probe('video', lambda *a, **k: Response(b'not-json'))
        self.assertEqual(r['status'], 'error')

    def test_disabled_route_and_corrupt_asset_never_send(self):
        self.store.is_disabled = lambda ident: True
        for purpose in probes.SAMPLES:
            self.assertEqual(self.run_probe(purpose, None)['reason'], 'route_unavailable')
        self.store.is_disabled = lambda ident: False
        with patch.object(probes, '_asset', side_effect=probes._ProbeError('asset_invalid')):
            self.assertEqual(self.run_probe('image', None)['reason'], 'asset_invalid')
            self.assertEqual(self.run_probe('audio', None)['reason'], 'asset_invalid')

    def test_http_errors_timeout_and_size_are_not_capability_failure(self):
        for purpose in ('tool', 'image', 'video', 'speech', 'audio'):
            for code, reason in [(403, 'authentication'), (429, 'rate_limit'), (503, 'server')]:
                def reject(request, timeout):
                    raise HTTPError(request.full_url, code, 'synthetic-provider-secret', {},
                                    io.BytesIO(b'synthetic-provider-secret'))
                r = self.run_probe(purpose, reject)
                self.assertEqual((r['status'], r['reason']), ('error', reason))
            def timeout(*a, **k):
                raise TimeoutError('synthetic-provider-secret')
            self.assertEqual(self.run_probe(purpose, timeout)['reason'], 'timeout')
            oversized = lambda *a, **k: Response(b'x' * (probes.MAX_RESPONSE_BYTES + 1))
            self.assertEqual(self.run_probe(purpose, oversized)['reason'], 'response_too_large')

    def test_native_media_real_inspector_and_independent_scopes(self):
        for purpose, (filename, _, expected) in probes.MEDIA.items():
            def native(request, timeout):
                self.assertIn('/v1beta/models/synthetic-model:generateContent', request.full_url)
                self.assertEqual(request.get_header('X-goog-api-key'), 'synthetic-provider-secret')
                body = json.loads(request.data)
                self.assertNotIn('reasoning', body)
                self.assertNotIn('thinkingConfig', body['generationConfig'])
                data = body['contents'][0]['parts'][1]['inlineData']['data']
                self.assertEqual(base64.b64decode(data), probes._asset(filename))
                return Response(json.dumps({'candidates': [{'finishReason': 'STOP',
                    'content': {'parts': [{'text': json.dumps(expected)}]}}]}).encode())
            r = self.run_probe(purpose, native, effort='max')
            self.assertEqual((r['status'], r['channel'], r['effort']), ('passed', 'gemini_media', None))
            self.assertEqual(r['scope'], {'speech': 'transcription_sample', 'video': 'visual_sample',
                                          'audio': 'non_speech_sample'}[purpose])

    def test_injected_inspector_and_explicit_audio_inability(self):
        def inspector(profile, secret, model, file_path, question, *, transport):
            self.assertEqual(Path(file_path).name, 'tone-sample.wav')
            self.assertNotIn('tone_count": 3', question)
            return {'ok': True, 'answer': '{"unavailable":true}'}
        r = self.run_probe('audio', None, media_inspector=inspector)
        self.assertEqual((r['status'], r['reason']), ('failed', 'semantic_mismatch'))
        r = self.run_probe('speech', None, media_inspector=lambda *a, **k: {
            'ok': True, 'answer': '{"number":417,"code":"海风"}'})
        self.assertEqual(r['scope'], 'transcription_sample')

    def test_non_native_media_and_missing_binding_never_send(self):
        self.store.profile['protocol'] = 'chat_completions'
        def forbidden(*a, **k):
            self.fail('must not send')
        for purpose in probes.MEDIA:
            self.assertEqual(self.run_probe(purpose, forbidden)['reason'], 'unsupported_path')
        self.store.get_engine_binding = lambda alias: None
        self.assertEqual(self.run_probe('tool', forbidden)['reason'], 'route_unavailable')

    def test_invalid_inputs_and_injected_error_do_not_leak(self):
        for purpose in ('connection', None, []):
            self.assertEqual(probes.probe_capability(self.store, 'sample-alias', purpose)['status'], 'error')
        self.assertEqual(probes.probe_capability(self.store, 'sample-alias', 'image', effort='bogus')['reason'], 'invalid_argument')
        def broken(*a, **k):
            raise RuntimeError('synthetic-provider-secret')
        self.assertEqual(self.run_probe('tool', broken)['reason'], 'unknown')

    def test_direct_provider_model_without_alias(self):
        self.store.get_route = lambda alias: self.fail('direct media must not resolve alias')
        self.store.get_engine_binding = lambda alias: self.fail('direct media must not bind alias')
        def inspector(profile, secret, model, file_path, question, *, transport):
            self.assertEqual(model, 'other-model')
            self.assertEqual(profile['model'], 'other-model')
            return {'ok': True, 'answer': '{"number":417,"code":"海风"}'}
        r = probes.probe_gemini_media(self.store, 'synthetic-id', 'other-model', 'speech', media_inspector=inspector)
        self.assertEqual((r['status'], r['channel'], r['effort']), ('passed', 'gemini_media', None))
        for provider, model, purpose in [('missing', 'other-model', 'speech'),
                                         ('synthetic-id', 'missing', 'video'),
                                         ('synthetic-id', 'other-model', 'tool')]:
            r = probes.probe_gemini_media(self.store, provider, model, purpose, media_inspector=inspector)
            self.assertEqual(r['status'], 'error')
            self.assertEqual(r['channel'], 'gemini_media')

    def test_remaining_budget_updates_socket_before_each_read(self):
        now, timeouts = [100.0], []
        owner = self
        class SlowResponse(Response):
            def __init__(self):
                super().__init__(b'x')
                self.fp = SimpleNamespace(raw=SimpleNamespace(
                    _sock=SimpleNamespace(settimeout=timeouts.append)))
                self.reads = 0

            def read1(self, size):
                self.reads += 1
                if self.reads == 1:
                    now[0] = 109.9
                    return b'x'
                owner.assertAlmostEqual(timeouts[-1], 0.1)
                raise TimeoutError('synthetic timeout')

        def connect(request, timeout):
            self.assertEqual(timeout, 10)
            now[0] = 102.0  # connection/header time is part of the same budget
            return SlowResponse()
        with patch.object(probes.time, 'monotonic', side_effect=lambda: now[0]):
            result = self.run_probe('image', connect, deadline=110.0)
        self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
        self.assertEqual(len(timeouts), 2)
        self.assertEqual(timeouts[0], 8)
        self.assertAlmostEqual(timeouts[1], 0.1)

    def test_two_tool_turns_share_explicit_and_default_budget(self):
        for explicit, budget in [(True, 10), (False, probes.REQUEST_TIMEOUT)]:
            with self.subTest(explicit=explicit):
                self.calls.clear()
                now, timeouts = [100.0], []
                def transport(request, timeout):
                    timeouts.append(timeout)
                    response = self.gateway(request, timeout)
                    now[0] += budget - 1 if len(timeouts) == 1 else 0
                    return response
                with patch.object(probes.time, 'monotonic', side_effect=lambda: now[0]):
                    kwargs = {'deadline': 100.0 + budget} if explicit else {}
                    result = self.run_probe('tool', transport, **kwargs)
                self.assertEqual(result['status'], 'passed')
                self.assertEqual(timeouts, [budget, 1])

    def test_expired_between_tool_turns_never_sends_second(self):
        now = [100.0]
        def receipt(*args):
            now[0] = 110.0
            return 'synthetic-receipt'
        with patch.object(probes.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(probes.secrets, 'token_hex', side_effect=receipt):
            result = self.run_probe('tool', self.gateway, deadline=110.0)
        self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
        self.assertEqual(len(self.calls), 1)

    def test_expired_deadline_never_invokes_transport_or_inspector(self):
        def forbidden(*args, **kwargs):
            self.fail('expired probe must not send')
        with patch.object(probes.time, 'monotonic', return_value=100):
            for purpose in probes.SAMPLES:
                result = self.run_probe(purpose, forbidden, deadline=100,
                                        media_inspector=forbidden)
                self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
            for fn, args in [
                (probes.probe_gemini_media, (self.store, 'synthetic-id', 'synthetic-model', 'speech')),
                (probes.probe_gemini_snapshot, (self.store, self.store.profile,
                    'synthetic-provider-secret', 'synthetic-model', 'speech')),
            ]:
                result = fn(*args, deadline=99, transport=forbidden, media_inspector=forbidden)
                self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))

    def test_connection_and_read_overrun_stop_without_followup(self):
        for phase in ('connect', 'read'):
            now, reads, opened = [100.0], [], []
            class LateResponse(Response):
                def read1(self, size):
                    reads.append(size)
                    now[0] = 111
                    return super().read1(size)
            def transport(request, timeout):
                opened.append(request.full_url)
                if phase == 'connect':
                    now[0] = 111
                return LateResponse(sse([{'type': 'function_call', 'name': 'capability_lookup',
                    'call_id': 'c', 'arguments': '{"key":"sample"}'}]))
            with patch.object(probes.time, 'monotonic', side_effect=lambda: now[0]):
                result = self.run_probe('tool', transport, deadline=110)
            self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
            self.assertEqual(len(opened), 1)
            self.assertEqual(len(reads), 0 if phase == 'connect' else 1)

    def test_each_request_keeps_25_second_ceiling(self):
        timeouts = []
        def transport(request, timeout):
            timeouts.append(timeout)
            return Response(sse([message('{"left":"red","right":"blue"}')]))
        with patch.object(probes.time, 'monotonic', return_value=100):
            result = self.run_probe('image', transport, deadline=1000)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(timeouts, [probes.REQUEST_TIMEOUT])

    def test_media_and_snapshot_forward_budget_and_timeout_without_leak(self):
        for snapshot in (False, True):
            now, timeouts = [100.0], []
            def transport(request, timeout):
                timeouts.append(timeout)
                raise TimeoutError('synthetic-provider-secret')
            def inspector(*args, transport):
                now[0] = 109.5  # local preparation must consume the shared budget
                return probes.gemini_media.inspect_media(*args, transport=transport)
            with patch.object(probes.time, 'monotonic', side_effect=lambda: now[0]):
                if snapshot:
                    result = probes.probe_gemini_snapshot(self.store, self.store.profile,
                        'synthetic-provider-secret', 'synthetic-model', 'speech', deadline=110,
                        transport=transport, media_inspector=inspector)
                else:
                    result = probes.probe_gemini_media(self.store, 'synthetic-id',
                        'synthetic-model', 'speech', deadline=110, transport=transport,
                        media_inspector=inspector)
            self.assertEqual(timeouts, [0.5])
            self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
            self.assertNotIn('synthetic-provider-secret', str(result))

    def test_snapshot_real_store_edit_keeps_original_wire_connection_and_key(self):
        import storage
        # Real SQLite store, disposable root, reversible fake encryption; no DPAPI.
        with tempfile.TemporaryDirectory(prefix='hub-probe-snapshot-') as root, \
             patch.object(storage, '_dpapi', side_effect=lambda data, decrypt=False: data):
            store = storage.ProfileStore(root)
            saved = store.save('Fixture', 'gemini_native', 'https://old.synthetic.invalid',
                               'old-model', 'synthetic-old-key')
            profile, secret = store.resolve_profile(saved['id'])
            store.save('Fixture changed', 'gemini_native', 'https://new.synthetic.invalid',
                       'new-model', 'synthetic-new-key', provider_id=saved['id'])
            latest, latest_key = store.resolve_profile(saved['id'])
            self.assertEqual(latest['base_url'], 'https://new.synthetic.invalid')
            self.assertEqual(latest_key, 'synthetic-new-key')
            requests = []
            def transport(request, timeout):
                requests.append(request)
                self.assertEqual(request.full_url,
                    'https://old.synthetic.invalid/v1beta/models/old-model:generateContent')
                self.assertEqual(request.get_header('X-goog-api-key'), 'synthetic-old-key')
                return Response(json.dumps({'candidates': [{'finishReason': 'STOP',
                    'content': {'parts': [{'text': '{"number":417,"code":"海风"}'}]}}]}).encode())
            with patch.object(store, 'resolve_profile', side_effect=AssertionError('must not resolve')), \
                 patch.object(store, 'resolve_route', side_effect=AssertionError('must not resolve')):
                result = probes.probe_gemini_snapshot(store, profile, secret, 'old-model',
                                                      'speech', transport=transport)
            self.assertEqual(result['status'], 'passed')
            self.assertEqual(len(requests), 1)
            self.assertEqual(set(result), {'status', 'reason', 'channel', 'effort', 'sample_id', 'scope'})
            for value in ('synthetic-old-key', 'synthetic-new-key', '417', '海风'):
                self.assertNotIn(value, str(result))

    def test_snapshot_matches_media_results_and_checks_disabled_before_send(self):
        for purpose in probes.MEDIA:
            for answer in (json.dumps(probes.MEDIA[purpose][2]), '{"unavailable":true}', 'bad json'):
                inspector = lambda *args, **kwargs: {'ok': True, 'answer': answer}
                snapshot = probes.probe_gemini_snapshot(self.store, self.store.profile,
                    'synthetic-provider-secret', 'synthetic-model', purpose, media_inspector=inspector)
                direct = probes.probe_gemini_media(self.store, 'synthetic-id',
                    'synthetic-model', purpose, media_inspector=inspector)
                self.assertEqual(snapshot, direct)
        def inspector(*args, transport):
            self.store.is_disabled = lambda ident: True
            return probes.gemini_media.inspect_media(*args, transport=transport)
        def forbidden(*args, **kwargs):
            self.fail('disabled snapshot must not send')
        result = probes.probe_gemini_snapshot(self.store, self.store.profile,
            'synthetic-provider-secret', 'synthetic-model', 'speech',
            media_inspector=inspector, transport=forbidden)
        self.assertEqual((result['status'], result['reason']), ('error', 'route_unavailable'))

    def test_production_health_requires_strict_guard_and_auth_before_post(self):
        owner, posts, methods = self, [], []
        guard = [{}]
        status = [200]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                methods.append('GET')
                owner.assertEqual(self.path, '/v1/_provider_hub/health')
                owner.assertEqual(self.headers['X-Provider-Hub-Token'], 'synthetic-gateway-secret')
                raw = json.dumps(guard[0]).encode()
                self.send_response(status[0]); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def do_POST(self):
                methods.append('POST'); posts.append(self.path)
                self.rfile.read(int(self.headers['Content-Length']))
                raw = sse([message('{"left":"red","right":"blue"}')])
                self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with patch('ocx_service._state', return_value={'port': server.server_port}):
                for value in ({}, {'capability_binding_guard': False},
                              {'capability_binding_guard': 1}, {'capability_binding_guard': 'true'},
                              {'capability_binding_guard': True},
                              {'capability_binding_guard': True, 'capability_deadline_guard': 1}):
                    guard[0] = value
                    self.assertEqual(self.run_probe('image', None)['reason'], 'route_unavailable')
                status[0] = 401
                guard[0] = {'capability_binding_guard': True, 'capability_deadline_guard': True}
                self.assertEqual(self.run_probe('image', None)['reason'], 'configuration')
                self.assertEqual(posts, [])
                status[0] = 200
                self.assertEqual(self.run_probe('image', None)['status'], 'passed')
                self.assertEqual(len(posts), 1)
                self.assertEqual(methods[-2:], ['GET', 'POST'])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_production_two_tool_turns_and_health_share_deadline(self):
        posts, workers, deadlines = [], [], []
        stop = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                raw = b'{"capability_binding_guard":true,"capability_deadline_guard":true}'
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def do_POST(self):
                posts.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                try:
                    if len(posts) == 1:
                        stop.wait(0.15)
                        raw = sse([{'type': 'function_call', 'name': 'capability_lookup',
                                    'call_id': 'c', 'arguments': '{"key":"sample"}'}])
                        self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers(); self.wfile.write(raw)
                    else:
                        self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: '); self.wfile.flush()
                        while not stop.wait(0.03):
                            self.wfile.write(b'x'); self.wfile.flush()
                except OSError:
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        original = probes.subprocess.Popen
        def spawn(*args, **kwargs):
            worker = original(*args, **kwargs); workers.append(worker)
            write = worker.stdin.write
            def observe(data):
                deadlines.append(json.loads(data)['deadline'])
                return write(data)
            worker.stdin.write = observe
            return worker
        try:
            start = time.monotonic(); deadline = start + 1.2
            with patch('ocx_service._state', return_value={'port': server.server_port}), \
                 patch.object(probes.subprocess, 'Popen', side_effect=spawn):
                result = self.run_probe('tool', None, deadline=deadline)
            self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
            self.assertEqual(len(posts), 2)
            self.assertEqual(deadlines, [deadline] * 4)
            self.assertTrue(all(w.poll() is not None for w in workers))
            self.assertTrue(all(w.stdin.closed and w.stdout.closed for w in workers))
            self.assertLess(time.monotonic() - start, 2.8)
        finally:
            stop.set(); server.shutdown(); server.server_close(); thread.join()

    @unittest.skipUnless(os.name == 'nt', 'Exercise real Windows anonymous-pipe backpressure')
    def test_windows_nonreading_worker_cannot_block_stdin_deadline(self):
        workers, writers = [], []
        writing = threading.Event()
        popen, thread_type = probes.subprocess.Popen, threading.Thread
        def spawn(args, **kwargs):
            self.assertNotIn('synthetic-private-key', str(args))
            worker = popen([args[0], '-B', '-c', 'import time; time.sleep(30)'], **kwargs)
            workers.append(worker)
            write = worker.stdin.write
            def observe(data):
                self.assertGreater(len(data), 1024 * 1024)
                writing.set()
                return write(data)
            worker.stdin.write = observe
            return worker
        def new_thread(*args, **kwargs):
            thread = thread_type(*args, **kwargs)
            if kwargs.get('name') == 'capability-probe-stdin':
                writers.append(thread)
            return thread
        request = probes.Request('http://127.0.0.1/never-sent', data=b'x' * (2 * 1024 * 1024),
                                 headers={'Authorization': 'Bearer synthetic-private-key'})
        start = time.monotonic()
        with patch.object(probes.subprocess, 'Popen', side_effect=spawn), \
             patch.object(probes.threading, 'Thread', side_effect=new_thread):
            with self.assertRaises(probes._ProbeError) as error:
                probes._open_bounded(request, None, deadline=start + 0.5)
        self.assertEqual(error.exception.reason, 'timeout')
        self.assertLess(time.monotonic() - start, 2.5)
        self.assertTrue(writing.is_set())
        self.assertEqual(len(workers), 1)
        self.assertIsNotNone(workers[0].poll())
        self.assertTrue(workers[0].stdin.closed and workers[0].stdout.closed)
        self.assertEqual(len(writers), 1)
        self.assertFalse(writers[0].is_alive())

    def test_worker_ipc_never_returns_echoed_credentials(self):
        request = {'url': 'http://127.0.0.1/unused', 'method': 'POST',
                   'headers': {'Authorization': 'Bearer synthetic-private-key'},
                   'data': '', 'deadline': time.monotonic() + 5, 'streaming': False}
        for raw in (b'synthetic-private-key', b'Bearer synthetic-private-key', b'valid-result'):
            output = io.BytesIO()
            with patch.object(probes.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))), \
                 patch.object(probes.sys, 'stdout', SimpleNamespace(buffer=output)), \
                 patch.object(probes, '_open_direct', return_value=raw):
                probes._request_worker()
            payload = output.getvalue()
            self.assertNotIn(b'synthetic-private-key', payload)
            value = json.loads(payload)
            if raw == b'valid-result':
                self.assertEqual(base64.b64decode(value['data']), raw)
            else:
                self.assertEqual(value, {'reason': 'upstream_rejected'})

    def test_binding_snapshot_rechecked_on_every_turn_and_explicit_start(self):
        for explicit in (False, True):
            self.calls.clear()
            self.store = FakeStore()
            expected = self.store.get_engine_binding('sample-alias')
            def change_after_first(request, timeout):
                self.assertEqual(request.get_header('X-provider-hub-expected-revision'),
                                 'synthetic-revision-A')
                self.assertEqual(request.get_header('X-provider-hub-expected-account'),
                                 'synthetic/account-A')
                response = self.gateway(request, timeout)
                self.store.profile.update(_revision='synthetic-revision-B', account='synthetic/account-B')
                return response
            result = self.run_probe('tool', change_after_first,
                **({'expected_binding': expected} if explicit else {}))
            self.assertEqual((result['status'], result['reason']), ('error', 'route_unavailable'))
            self.assertEqual(len(self.calls), 1)
            def forbidden(*a, **k):
                self.fail('stale explicit binding must not send')
            result = self.run_probe('tool', forbidden, expected_binding=expected)
            self.assertEqual(result['reason'], 'route_unavailable')

    def test_default_worker_killed_and_reaped_during_dripping_headers(self):
        requests, workers = [], []
        disconnected, stop = threading.Event(), threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                raw = b'{"capability_binding_guard":true,"capability_deadline_guard":true}'
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def do_POST(self):
                requests.append(self.path)
                self.rfile.read(int(self.headers['Content-Length']))
                try:
                    self.wfile.write(b'HTTP/1.1 200 OK\r\nX-Slow: ')
                    self.wfile.flush()
                    while not stop.wait(0.03):
                        self.wfile.write(b'x')
                        self.wfile.flush()
                except OSError:
                    disconnected.set()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = probes.subprocess.Popen
        def spawn(args, **kwargs):
            self.assertNotIn('synthetic-gateway-secret', str(args))
            self.assertIs(kwargs['stdin'], subprocess.PIPE)
            self.assertIs(kwargs['stderr'], subprocess.DEVNULL)
            worker = original(args, **kwargs)
            workers.append(worker)
            return worker
        try:
            start = time.monotonic()
            with patch('ocx_service._state', return_value={'port': server.server_port}), \
                 patch.object(probes.subprocess, 'Popen', side_effect=spawn):
                result = self.run_probe('tool', None, deadline=start + 0.6)
            self.assertEqual((result['status'], result['reason']), ('error', 'timeout'))
            self.assertLess(time.monotonic() - start, 2.5)
            self.assertEqual(len(workers), 2)  # authenticated health + one POST
            self.assertTrue(all(w.poll() is not None for w in workers))
            self.assertTrue(all(w.stdin.closed and w.stdout.closed for w in workers))
            self.assertTrue(disconnected.wait(2))
            self.assertEqual(len(requests), 1)  # no second tool request
        finally:
            stop.set()
            server.shutdown(); server.server_close(); thread.join()

    def test_default_worker_delayed_dns_cannot_send_after_timeout(self):
        requests, workers = [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                raw = b'{"capability_binding_guard":true,"capability_deadline_guard":true}'
                self.send_response(200); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def do_POST(self):
                requests.append(self.path)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = probes.subprocess.Popen
        def delayed_dns(args, **kwargs):
            # Delay inside the real worker before its first connect. No file or
            # credentials in argv; original payload still uses private stdin.
            script = ("import socket,time,runpy,sys; original=socket.getaddrinfo; "
                      "socket.getaddrinfo=lambda *a,**k: (time.sleep(1.0),original(*a,**k))[1]; "
                      "sys.argv=[sys.argv[1],'--bounded-request']; "
                      "runpy.run_path(sys.argv[0],run_name='__main__')")
            worker = original([args[0], '-B', '-c', script, args[2]], **kwargs)
            workers.append(worker)
            return worker
        try:
            start = time.monotonic()
            with patch('ocx_service._state', return_value={'port': server.server_port}), \
                 patch.object(probes.subprocess, 'Popen', side_effect=delayed_dns):
                result = self.run_probe('tool', None, deadline=start + 0.4)
            self.assertEqual(result['reason'], 'timeout')
            self.assertEqual(len(workers), 1)
            self.assertIsNotNone(workers[0].poll())
            threading.Event().wait(1.1)  # past the delayed DNS release time
            self.assertEqual(requests, [])
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_asset_integrity_png_pixels_and_tone_ground_truth(self):
        manifest = json.loads((probes.ASSETS / 'manifest.json').read_text())
        for name in manifest:
            probes._asset(name)
        png = probes._asset('two-colour.png')
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('!II', png[16:24]), (64, 32))
        offset, compressed = 8, b''
        while offset < len(png):
            size = struct.unpack('!I', png[offset:offset+4])[0]
            kind, data = png[offset+4:offset+8], png[offset+8:offset+8+size]
            self.assertEqual(zlib.crc32(kind+data)&0xffffffff,
                             struct.unpack('!I',png[offset+8+size:offset+12+size])[0])
            if kind == b'IDAT':
                compressed += data
            offset += size+12
        row = b'\0'+b'\xff\0\0'*32+b'\0\0\xff'*32
        self.assertEqual(zlib.decompress(compressed), row*32)
        with wave.open(str(probes.ASSETS / 'tone-sample.wav')) as w:
            rate = w.getframerate()
            values = struct.unpack('<'+'h'*w.getnframes(), w.readframes(w.getnframes()))
        starts = [i for i,v in enumerate(values) if abs(v)>1000 and (i==0 or not any(abs(x)>1000 for x in values[max(0,i-rate//10):i]))]
        self.assertEqual(len(starts), 3)
        frequencies = []
        for start in starts:
            v = values[start:start+rate//4]
            frequencies.append(sum(v[i-1]<0<=v[i] for i in range(1,len(v)))*4)
        self.assertTrue(420<frequencies[0]<460 and 630<frequencies[1]<690 and 850<frequencies[2]<910)

    def test_loopback_fake_gateway_uses_injected_http_transport(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                owner.assertEqual(self.path, '/v1/responses')
                owner.assertEqual(self.headers['X-Provider-Hub-Token'], 'synthetic-gateway-secret')
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.assertEqual(body['model'], 'sample-alias')
                raw = sse([message('{"left":"red","right":"blue"}')])
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            real_read_timeout, socket_timeouts = probes._read_timeout, []
            def check_socket(response, timeout):
                real_read_timeout(response, timeout)
                if getattr(response, 'fp', None) is not None:  # EOF or injected health BytesIO
                    socket_timeouts.append(response.fp.raw._sock.gettimeout())
                    self.assertAlmostEqual(socket_timeouts[-1], timeout)
            with patch('ocx_service._state', return_value={'port': server.server_port}), \
                 patch.object(probes, '_read_timeout', side_effect=check_socket):
                self.assertEqual(self.run_probe('image', probes.build_opener(probes.ProxyHandler({}),
                    probes._NoRedirect()).open)['status'], 'passed')
            self.assertTrue(socket_timeouts)
            self.assertTrue(all(0 < t <= probes.REQUEST_TIMEOUT for t in socket_timeouts))
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


if __name__ == '__main__':
    unittest.main()
