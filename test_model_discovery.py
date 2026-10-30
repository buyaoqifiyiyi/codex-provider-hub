import io,json,os,tempfile,unittest
from contextlib import closing
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

# Import-time defaults must also stay inside a disposable environment.
with tempfile.TemporaryDirectory(prefix='hub-neutral-import-') as import_root:
    with patch.dict(os.environ, {'LOCALAPPDATA':import_root, 'CODEX_HOME':str(Path(import_root)/'codex'), 'HF_HUB_OFFLINE':'1'}):
        import mcp_server
from probe import fetch_model_ids
from storage import ProfileStore


class Response(io.BytesIO): status=200


class ModelListTests(unittest.TestCase):
    def test_provider_models_are_filtered_deduplicated_and_never_echo_credentials(self):
        seen=[]
        class Opener:
            def open(self,request,timeout):
                seen.append((request.full_url,dict(request.header_items())))
                return Response(json.dumps({'data':[{'id':'a'},{'id':'vendor/model'},{'id':'a'},
                                                   {'id':'bad model'}, {'id':'b\nsecret'}]}).encode())
        result=fetch_model_ids('https://example.invalid/v1','synthetic-key',opener=Opener())
        self.assertEqual(result,{'ok':True,'models':[{'id':'a'},{'id':'vendor/model'}]})
        self.assertEqual(seen[0][0],'https://example.invalid/v1/models')
        self.assertNotIn('synthetic-key',str(result))

    def test_missing_malformed_and_oversized_lists_do_not_invent_names(self):
        class Opener:
            def __init__(self,body):self.body=body
            def open(self,request,timeout):return Response(self.body)
        for body in (b'{"data":[]}',b'{"data":"bad"}',b'invalid',b' '*(4*1024*1024+1)):
            self.assertFalse(fetch_model_ids('https://example.invalid/v1','key',opener=Opener(body))['ok'])

    def test_root_endpoint_tries_v1_only_after_not_found_on_same_origin(self):
        urls=[]
        class Opener:
            def open(self,request,timeout):
                urls.append(request.full_url)
                if len(urls)==1:raise HTTPError(request.full_url,404,'not found',None,None)
                return Response(b'{"data":[{"id":"model-a"}]}')
        result=fetch_model_ids('https://example.invalid','key',opener=Opener())
        self.assertTrue(result['ok'])
        self.assertEqual(urls,['https://example.invalid/models','https://example.invalid/v1/models'])

    def test_compatibility_suffix_falls_back_without_changing_origin(self):
        urls=[]
        class Opener:
            def open(self,request,timeout):
                urls.append(request.full_url)
                if len(urls)<3: raise HTTPError(request.full_url,404,'not found',None,None)
                return Response(b'{"models":[{"slug":"model-a","owned_by":"Acme","context_window":128000}]}')
        result=fetch_model_ids('https://example.invalid/apps/anthropic','synthetic-key',opener=Opener())
        self.assertTrue(result['ok'])
        self.assertEqual(urls[-1],'https://example.invalid/v1/models')
        self.assertTrue(all(url.startswith('https://example.invalid/') for url in urls))
        self.assertEqual(result['models'],[{'id':'model-a','owned_by':'Acme','context_window':128000}])
        self.assertNotIn('synthetic-key',str(result))

    def test_provider_owner_and_explicit_capabilities_are_returned_without_secret(self):
        class Opener:
            def open(self,request,timeout):
                return Response(json.dumps({'data':[{'id':'model-a','owned_by':'synthetic-key',
                    'supported_reasoning_levels':['high'],'modalities':{'input':['text','image']}}]}).encode())
        result=fetch_model_ids('https://example.invalid/v1','synthetic-key',opener=Opener())
        self.assertEqual(result['models'],[{'id':'model-a','reasoning_levels':['high'],'supports_image':True}])
        self.assertNotIn('synthetic-key',str(result))

    def test_reference_only_fills_missing_fields_and_keeps_source(self):
        class Opener:
            def open(self,request,timeout):
                return Response(b'{"data":[{"id":"model-a","context_window":128000}]}')
        def reference(base, ids):
            return {'model-a': {'context_window': 200000, 'reasoning_levels': ['high'], 'supports_image': True,
                                'sources': {'context_window':'models.dev:vendor-reference',
                                            'reasoning_levels':'models.dev:same-provider',
                                            'supports_image':'models.dev:vendor-reference'}}}
        result=fetch_model_ids('https://example.invalid/v1','synthetic-key',opener=Opener(),reference_lookup=reference)
        item=result['models'][0]
        self.assertEqual(item['context_window'],128000)
        self.assertEqual(item['reasoning_levels'],['high'])
        self.assertTrue(item['supports_image'])
        self.assertEqual(item['reference_sources'],{'reasoning_levels':'models.dev:same-provider',
                                                    'supports_image':'models.dev:vendor-reference'})

    def test_unavailable_reference_does_not_block_model_discovery(self):
        class Opener:
            def open(self,request,timeout): return Response(b'{"data":[{"id":"model-a"}]}')
        def unavailable(*_): raise OSError('offline')
        result=fetch_model_ids('https://example.invalid/v1','synthetic-key',opener=Opener(),reference_lookup=unavailable)
        self.assertEqual(result,{'ok':True,'models':[{'id':'model-a'}]})

    def test_large_lists_report_when_the_display_limit_is_reached(self):
        class Opener:
            def open(self,request,timeout):
                return Response(json.dumps({'data':[{'id':'model-'+str(i)} for i in range(5001)]}).encode())
        result=fetch_model_ids('https://example.invalid/v1','key',opener=Opener())
        self.assertEqual(len(result['models']),5000)
        self.assertTrue(result['truncated'])


@unittest.skipUnless(os.name=='nt','Windows DPAPI is required')
class DiscoveryToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='hub-model-list-')
        self.env=patch.dict(os.environ,{'LOCALAPPDATA':self.tmp.name});self.env.start()
        from test_neutral_python_compat import isolated_native_catalog
        isolated_native_catalog(self, self.tmp.name)
        self.store=ProfileStore()
    def tearDown(self):self.env.stop();self.tmp.cleanup()

    def test_saved_key_only_used_for_same_origin_and_protocol(self):
        p=self.store.save('Fixture','responses','https://example.invalid/v1','model-a','synthetic-key')
        with patch('probe.fetch_model_ids',return_value={'ok':True,'models':[{'id':'model-a'}]}) as fetch:
            result=mcp_server.fetch_models('responses','https://example.invalid/v2','',p['id'])
            self.assertTrue(result['ok']);self.assertEqual(fetch.call_args.args,('https://example.invalid/v2','synthetic-key'))
            before=fetch.call_count
            result=mcp_server.fetch_models('responses','https://other.invalid/v1','',p['id'])
            self.assertFalse(result['ok']);self.assertEqual(fetch.call_count,before)
            result=mcp_server.fetch_models('chat_completions','https://example.invalid/v1','',p['id'])
            self.assertFalse(result['ok']);self.assertEqual(fetch.call_count,before)
        self.assertEqual(len(self.store.list()),1)
        self.assertNotIn('synthetic-key',str(result))

    def test_disabled_profile_never_queries_provider(self):
        p=self.store.save('Fixture','responses','https://example.invalid/v1','model-a','synthetic-key')
        with closing(self.store._connect()) as db, db:
            db.execute('INSERT INTO disabled_connections(provider_id) VALUES (?)',(p['id'],))
        with patch('probe.fetch_model_ids') as fetch:
            result=mcp_server.fetch_models('responses','https://example.invalid/v1','',p['id'])
        self.assertFalse(result['ok']);fetch.assert_not_called()

    def test_structured_endpoint_and_official_local_catalog(self):
        p=self.store.save('Structured service','system_one','https://example.invalid/v1/systemone','structured-fixture','synthetic-key')
        with patch('probe.fetch_model_ids',return_value={'ok':True,'models':[{'id':'structured-fixture'}]}) as fetch:
            self.assertTrue(mcp_server.fetch_models('system_one','https://example.invalid/v1/systemone','',p['id'])['ok'])
            self.assertEqual(fetch.call_args.args[0],'https://example.invalid/v1')
        (self.store.root/'baseline-catalog.json').write_text(json.dumps({'models':[{'slug':'native-alpha'},
            {'slug':'vendor/other'},{'slug':'native-gamma','visibility':'hide'}]}),encoding='utf8')
        result=mcp_server.fetch_models('codex_subscription')
        self.assertEqual(result['models'],[{'id':'native-alpha'}])
        self.assertEqual(result['source'],'本机订阅模型目录')


if __name__=='__main__':unittest.main()
