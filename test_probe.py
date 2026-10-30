import io
import json
import os
import socket
import ssl
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from probe import MARKER, probe_effort, probe_snapshot, probe_jev, probe_official, discover_capabilities, official_capabilities, describe_probe_failure
from responses_adapter import convert_stream, UnsupportedSemantic
from storage import ProfileStore


class Reply(io.BytesIO):
    status = 200


class ProbeDiagnosticTests(unittest.TestCase):
    route={'protocol':'chat_completions','base_url':'https://example.invalid/v1','model':'fixture'}

    @staticmethod
    def stream(delta, finish='stop', *, matched=None, extra=None, done=True):
        choice={'index':0,'delta':delta,'finish_reason':finish,'matched_stop':matched}
        chunk={'object':'chat.completion.chunk','created':1,'choices':[choice]}
        if extra:chunk.update(extra)
        return ('data: '+json.dumps(chunk)+'\n\n'+('data: [DONE]\n\n' if done else '')).encode()

    def test_provider_that_needs_over_thirty_seconds_to_start_can_pass(self):
        data=self.stream({'content':MARKER})
        class SlowOpener:
            def open(self, request, timeout):
                if timeout < 45:
                    raise socket.timeout()
                return Reply(data)
        with patch('probe.urllib.request.build_opener',return_value=SlowOpener()):
            result=probe_snapshot(self.route,'synthetic-key',None)
        self.assertTrue(result['ok'],result)

    def test_stream_is_not_abandoned_at_thirty_seconds(self):
        import probe
        values=iter([0]+[31]*20)
        with patch('probe.time.monotonic',side_effect=lambda:next(values)):
            self.assertEqual(list(probe._bounded_lines(Reply(b'data: x\n\n'))),[b'data: x\n',b'\n'])

    def test_cache_statistics_work_in_probe_and_gateway(self):
        # Synthetic chat-completions usage with cache hit/miss counters.
        usage={'prompt_tokens':10,'completion_tokens':4,'total_tokens':14,
               'prompt_tokens_details':{'cached_tokens':4},
               'prompt_cache_hit_tokens':4,'prompt_cache_miss_tokens':6}
        chunk={'object':'chat.completion.chunk','created':1,
               'choices':[{'index':0,'delta':{'content':MARKER},'finish_reason':'stop'}], 'usage':usage}
        data=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()
        result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
        self.assertTrue(result['ok'],result)
        frames=list(convert_stream(iter(data.splitlines()),'fixture'))
        self.assertTrue(any(b'"cached_tokens":4' in frame for frame in frames))

    def test_service_tier_metadata_does_not_block_probe_or_gateway(self):
        for tier in (None, 'default', 'priority'):
            with self.subTest(tier=tier):
                chunk={'object':'chat.completion.chunk','created':1,'service_tier':tier,
                       'choices':[{'index':0,'delta':{'content':MARKER},'finish_reason':'stop'}]}
                data=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()
                result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
                self.assertTrue(result['ok'],result)
                frames=list(convert_stream(iter(data.splitlines()),'fixture'))
                if tier is not None:
                    self.assertTrue(any(('"service_tier":"'+tier+'"').encode() in frame for frame in frames))

    def test_invalid_service_tier_cannot_be_misreported_as_success(self):
        chunk={'object':'chat.completion.chunk','created':1,'service_tier':{'secret':'synthetic-secret'},
               'choices':[{'index':0,'delta':{'content':MARKER},'finish_reason':'stop'}]}
        data=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()
        result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
        self.assertFalse(result['ok'])
        self.assertEqual(result['detail'],'unsupported_service_tier')
        self.assertNotIn('synthetic-secret',str(result)+describe_probe_failure(result))

    def test_model_permission_denied_is_specific_without_returning_error_text(self):
        body=json.dumps({'error':{'message':'This token has no access to model fixture (request id: synthetic-secret)'}}).encode()
        def fail(*args):
            raise urllib.error.HTTPError('https://fixture.invalid',403,'Forbidden',None,io.BytesIO(body))
        result=probe_snapshot(self.route,'synthetic-key',None,transport=fail)
        self.assertEqual(result['code'],'model_access_denied')
        self.assertIn('没有访问该模型的权限',describe_probe_failure(result))
        self.assertNotIn('synthetic-secret',str(result)+describe_probe_failure(result))

    def test_cache_statistics_reject_inconsistent_counts(self):
        base={'prompt_tokens':10,'completion_tokens':4,'total_tokens':14,
              'prompt_tokens_details':{'cached_tokens':4},
              'prompt_cache_hit_tokens':4,'prompt_cache_miss_tokens':6}
        for changes in ({'prompt_cache_hit_tokens':-1}, {'prompt_cache_hit_tokens':True},
                        {'prompt_cache_miss_tokens':11}, {'prompt_cache_miss_tokens':5},
                        {'prompt_tokens_details':{'cached_tokens':3}}):
            with self.subTest(changes=changes):
                usage={**base,**changes}
                data=self.stream({'content':MARKER},extra={'usage':usage})
                result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
                self.assertFalse(result['ok'])
                self.assertEqual(result['detail'],'invalid_usage')

    def test_cache_hit_count_is_mapped_without_cached_details(self):
        usage={'prompt_tokens':10,'completion_tokens':4,'total_tokens':14,
               'prompt_cache_hit_tokens':4,'prompt_cache_miss_tokens':6}
        chunk={'object':'chat.completion.chunk','created':1,
               'choices':[{'index':0,'delta':{'content':MARKER},'finish_reason':'stop'}], 'usage':usage}
        data=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()
        frames=list(convert_stream(iter(data.splitlines()),'fixture'))
        self.assertTrue(any(b'"cached_tokens":4' in frame for frame in frames))

    def test_protocol_error_identifies_response_section_without_upstream_values(self):
        cases=[({'unknown_field':'synthetic-secret'},'unsupported_response_chunk_field'),
               ({'usage':{'prompt_tokens':1,'completion_tokens':1,'unknown_field':'synthetic-secret'}},
                'unsupported_usage_field')]
        for extra, detail in cases:
            with self.subTest(detail=detail):
                data=self.stream({'content':MARKER},extra=extra)
                result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
                self.assertEqual(result.get('detail'),detail)
                self.assertNotIn('synthetic-secret',str(result)+describe_probe_failure(result))

    def test_probe_accepts_only_known_matched_stop_metadata_but_normal_adapter_remains_strict(self):
        for matched in (None, 123, 'stop-token'):
            data=self.stream({'content':MARKER},matched=matched)
            result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
            self.assertTrue(result['ok'])
            with self.assertRaises(UnsupportedSemantic):
                list(convert_stream(iter(data.splitlines()),'fixture'))
        data=self.stream({'content':MARKER},matched={'unexpected':'synthetic-secret'})
        result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
        self.assertFalse(result['ok']);self.assertEqual(result['detail'],'invalid_matched_stop')
        self.assertNotIn('synthetic-secret',str(result))

    def test_distinguishes_output_limit_filter_missing_marker_and_protocol_errors(self):
        cases=[(self.stream({'reasoning_content':'synthetic'},'length'),'output_limit'),
               (self.stream({'content':MARKER},'length'),'output_limit'),
               (self.stream({'content':'synthetic'},'content_filter'),'content_filter'),
               (self.stream({'reasoning_content':'synthetic'},'stop'),'reasoning_only'),
               (self.stream({'content':'other'},'stop'),'marker_missing'),
               (self.stream({'content':MARKER},'stop',done=False),'response_stream_invalid'),
               (self.stream({'content':MARKER},'stop',extra={'unknown_field':'synthetic-secret'}),'response_protocol_unsupported')]
        for data,code in cases:
            with self.subTest(code=code):
                result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
                self.assertFalse(result['ok']);self.assertEqual(result['code'],code)
                self.assertNotIn('synthetic-secret',str(result)+describe_probe_failure(result))

    def test_http_dns_tls_and_timeout_messages_are_specific_without_error_bodies(self):
        errors=[(urllib.error.HTTPError('https://secret.invalid',401,'synthetic-secret',None,None),'upstream_rejected','401'),
                (urllib.error.URLError(socket.gaierror('synthetic-secret')),'dns_failed','域名'),
                (urllib.error.URLError(ssl.SSLError('synthetic-secret')),'tls_failed','证书'),
                (urllib.error.URLError(TimeoutError('synthetic-secret')),'network_timeout','超时')]
        for error,code,text in errors:
            with self.subTest(code=code):
                def fail(*args):raise error
                result=probe_snapshot(self.route,'synthetic-key',None,transport=fail)
                self.assertEqual(result['code'],code)
                message=describe_probe_failure(result)
                self.assertIn(text,message)
                self.assertNotIn('synthetic-secret',message+str(result))

    def test_stream_error_body_is_never_returned(self):
        data=b'data: {"error":{"code":"bad","message":"synthetic-secret"}}\n\ndata: [DONE]\n\n'
        result=probe_snapshot(self.route,'synthetic-key',None,transport=lambda *args:Reply(data))
        self.assertEqual(result['code'],'upstream_stream_error')
        self.assertNotIn('synthetic-secret',str(result)+describe_probe_failure(result))


class MetadataTests(unittest.TestCase):
    def test_discovery_uses_exact_model_and_only_advertised_capabilities(self):
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append((request.full_url, request.headers.get('Authorization')))
                model = {'id':'fixture', 'context_length':128000,
                         'supported_reasoning_levels':[{'effort':'medium'},{'effort':'high'}],
                         'default_reasoning_level':'high'}
                return Reply(json.dumps({'data':[model, {'id':'other','context_window':900000}]}).encode())
        result = discover_capabilities({'protocol':'responses','base_url':'https://example.invalid/v1','model':'fixture'},
                                       'synthetic-key', opener=Opener())
        self.assertEqual(result, {'context_window':128000,'reasoning_levels':['medium','high'],'default_effort':'high'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 'Bearer synthetic-key')

    def test_unknown_metadata_stays_unknown_and_advertised_efforts_are_preserved(self):
        class Opener:
            def __init__(self, item): self.item=item
            def open(self, request, timeout): return Reply(json.dumps({'data':[self.item]}).encode())
        route={'protocol':'chat_completions','base_url':'https://example.invalid/v1','model':'fixture-chat'}
        self.assertEqual(discover_capabilities(route,'key',opener=Opener({'id':'fixture-chat'})),{})
        item={'id':'fixture-chat','reasoning_efforts':['none','low','medium','high','xhigh','max','ultra'], 'default_reasoning_effort':'medium'}
        self.assertEqual(discover_capabilities(route,'key',opener=Opener(item)),
                         {'reasoning_levels':['none','low','medium','high','xhigh','max','ultra'],'default_effort':'medium'})

    def test_public_reference_fills_only_unknown_capabilities_and_is_identified(self):
        class Opener:
            def open(self, request, timeout):
                return Reply(json.dumps({'data':[{'id':'fixture','context_window':64000}]}).encode())
        def reference(base, ids):
            return {'fixture': {'context_window':128000, 'reasoning_levels':['high'],
                                'sources':{'context_window':'models.dev:vendor-reference',
                                           'reasoning_levels':'models.dev:same-provider'}}}
        route={'protocol':'responses','base_url':'https://example.invalid/v1','model':'fixture'}
        result=discover_capabilities(route,'synthetic-key',opener=Opener(),reference_lookup=reference)
        self.assertEqual(result,{'context_window':64000,'reasoning_levels':['high'],
                                 'reference_sources':{'reasoning_levels':'models.dev:same-provider'}})

    def test_structured_answer_requires_valid_success_payload(self):
        route={'model':'structured-fixture','base_url':'https://example.invalid/v1/systemone'}
        good=lambda *a: Reply(b'{"code":0,"data":{"answers":{"check":{"type":"noul","noul":0.9}}}}')
        bad=lambda *a: Reply(b'{"code":0,"data":{"answers":{}}}')
        self.assertTrue(probe_jev(route,'synthetic',transport=good)['ok'])
        self.assertFalse(probe_jev(route,'synthetic',transport=bad)['ok'])
        for value in ('true', 'NaN', '-0.1', '1.5', '"0.9"'):
            payload=('{"data":{"answers":{"check":{"noul":'+value+'}}}}').encode()
            self.assertFalse(probe_jev(route,'synthetic',transport=lambda *a: Reply(payload))['ok'])

    def test_nested_metadata_and_duplicate_model_ids(self):
        class Opener:
            def __init__(self, rows): self.rows=rows
            def open(self, request, timeout): return Reply(json.dumps({'data':self.rows}).encode())
        route={'protocol':'responses','base_url':'https://example.invalid/v1','model':'fixture'}
        row={'id':'fixture','metadata':{'limits':{'max_context_length':64000}},
             'reasoning_parameters':{'efforts':['low','high']}}
        self.assertEqual(discover_capabilities(route,'key',opener=Opener([row])),
                         {'context_window':64000,'reasoning_levels':['low','high']})
        self.assertEqual(discover_capabilities(route,'key',opener=Opener([row,row])),{})
        nested={'id':'fixture','metadata':{'context_length':96000,'capabilities':{
            'reasoning_parameters':{'efforts':['medium','high']}}}}
        self.assertEqual(discover_capabilities(route,'key',opener=Opener([nested])),
                         {'context_window':96000,'reasoning_levels':['medium','high']})
        nested['metadata']['capabilities']['supports_reasoning_effort']=False
        self.assertEqual(discover_capabilities(route,'key',opener=Opener([nested])),{'context_window':96000})

    def test_probe_rejects_truncated_and_oversized_streams(self):
        route={'protocol':'responses','base_url':'https://example.invalid/v1','model':'fixture'}
        event=('data: '+json.dumps({'type':'response.output_text.delta','delta':MARKER})+'\n\n').encode()
        self.assertFalse(probe_snapshot(route,'key',None,transport=lambda *a: Reply(event))['ok'])
        self.assertFalse(probe_snapshot(route,'key',None,transport=lambda *a: Reply(b'x'*70000))['ok'])
        completed=('data: '+json.dumps({'type':'response.completed'})+'\n\n'+'data: [DONE]\n\n').encode()
        self.assertTrue(probe_snapshot(route,'key',None,transport=lambda *a: Reply(event+completed))['ok'])

    def test_subscription_probe_uses_local_hub_and_does_not_expose_credentials(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); home=root/'codex';home.mkdir()
            (home/'auth.json').write_text(json.dumps({'auth_mode':'chatgpt','tokens':{
                'access_token':'synthetic-access','account_id':'synthetic-account'}}),encoding='utf8')
            calls=[]
            class Opener:
                def open(self, request, timeout):
                    calls.append((request.full_url,dict(request.header_items())))
                    events=[{'type':'response.output_text.delta','delta':MARKER},{'type':'response.completed'}]
                    return Reply(''.join('data: '+json.dumps(x)+'\n\n' for x in events).encode())
            import ocx_service
            ocx_service._create_admin_capability(root)
            result=probe_official('native-alpha',root,codex_home=home,
                service_status=lambda root:{'running':True,'official_route_ready':True,'port':12345},
                capability_reader=lambda root:'synthetic-capability',opener=Opener())
            self.assertTrue(result['ok'])
            self.assertNotIn('synthetic-access',str(result))
            self.assertEqual(calls[0][0],'http://127.0.0.1:12345/v1/responses')

    def test_official_catalog_metadata_is_attributed_to_local_catalog(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            (Path(root)/'baseline-catalog.json').write_text(json.dumps({'models':[
                {'slug':'native-alpha','context_window':1000000,'supported_reasoning_levels':[{'effort':'high'}]}]}),encoding='utf8')
            self.assertEqual(official_capabilities(Path(root),'native-alpha'),
                             {'context_window':1000000,'reasoning_levels':['high']})


@unittest.skipUnless(os.name == "nt", "Windows DPAPI is required")
class ProbeTests(unittest.TestCase):
    def test_only_verified_unchanged_credential_gets_effort_capability(self):
        with tempfile.TemporaryDirectory(prefix="provider-probe-test-") as root:
            store = ProfileStore(root)
            provider = store.save("External", "chat_completions", "https://example.invalid/v1", "fixture", "synthetic-key")
            store.set_route("fixture-route", provider["id"])
            observed = []
            def transport(url, body, headers):
                observed.append((url, json.loads(body), dict(headers)))
                chunk = {"object": "chat.completion.chunk", "created": 1, "choices": [
                    {"index": 0, "delta": {"content": MARKER}, "finish_reason": "stop"}]}
                return Reply(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())
            self.assertTrue(probe_effort(store, "fixture-route", "high", transport=transport)["ok"])
            route, _ = store.resolve_route("fixture-route")
            self.assertEqual(store.confirmed_efforts(route), {"high"})
            self.assertEqual(observed[0][1]["reasoning_effort"], "high")
            self.assertEqual(observed[0][2]["Authorization"], "Bearer synthetic-key")
            self.assertEqual(store.confirmed_efforts(route) & {"max"}, set())


if __name__ == "__main__":
    unittest.main()
