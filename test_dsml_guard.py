"""Synthetic loopback regression for the observed malformed final answer."""
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from front_proxy import _guard_dsml_for, guard_incomplete_dsml, make_front
from gateway import safe_responses_stream
from storage import ProfileStore

TOKEN = 'synthetic_local_capability_0123456789'
MODEL = 'fixture-model'
MARKERS = '</｜｜DSML｜｜ parameter>\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'


def frame(kind, **fields):
    event = {'type':kind, **fields}
    return ('event: '+kind+'\ndata: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode('utf-8')


def synthetic_stream(text, *, split=4, native_tool=False):
    response={'id':'synthetic-response','object':'response','status':'in_progress','output':[]}
    item={'id':'synthetic-message','type':'message','role':'assistant','status':'in_progress','content':[]}
    frames=[frame('response.created',response=response),
            frame('response.output_item.added',output_index=0,item=item),
            frame('response.content_part.added',item_id=item['id'],output_index=0,content_index=0,
                  part={'type':'output_text','text':'','annotations':[]})]
    for offset in range(0,len(text),split):
        frames.append(frame('response.output_text.delta',item_id=item['id'],output_index=0,content_index=0,
                            delta=text[offset:offset+split]))
    frames.append(frame('response.output_text.done',item_id=item['id'],output_index=0,content_index=0,text=text))
    native=None
    if native_tool:
        native={'id':'fc_fake','type':'function_call','name':'read_fixture','call_id':'call_fake',
                'arguments':'{}','status':'completed'}
        frames.append(frame('response.output_item.added',output_index=1,item=dict(native,status='in_progress',arguments='')))
        frames.append(frame('response.function_call_arguments.delta',item_id='fc_fake',output_index=1,delta='{}'))
        frames.append(frame('response.function_call_arguments.done',item_id='fc_fake',output_index=1,arguments='{}'))
        frames.append(frame('response.output_item.done',output_index=1,item=native))
    final={'id':'synthetic-response','object':'response','status':'completed','output':[
        {'id':item['id'],'type':'message','role':'assistant','status':'completed',
         'content':[{'type':'output_text','text':text,'annotations':[]}]}]}
    if native: final['output'].append(native)
    frames.append(frame('response.completed',response=final))
    return b''.join(frames)


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI is required')
class DsmlGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='provider-dsml-test-')
        self.root=Path(self.temp.name)
        from test_neutral_python_compat import isolated_native_catalog
        isolated_native_catalog(self,self.root)
        self.store=ProfileStore(self.root/'store')
        profile=self.store.save('fixture','chat_completions','https://example.invalid/v1',MODEL,'synthetic-key')
        alias_name='hub-'+profile['id'].replace('-','')
        self.alias=alias_name+'/'+MODEL
        account=alias_name+'/22222222-2222-4222-8222-222222222222'
        selected,_=self.store.resolve_profile(profile['id'])
        self.store.bind_engine_route(self.alias,profile['id'],selected['_revision'],account)
        self.engine_home=self.root/'engine';self.engine_home.mkdir()
        config={'defaultProvider':'openai','providers':{
            'openai':{'adapter':'openai-responses','baseUrl':'https://chatgpt.com/backend-api/codex',
                      'authMode':'forward','codexAccountMode':'direct'},
            alias_name:{'adapter':'openai-chat','baseUrl':'https://example.invalid/v1',
                        'defaultModel':MODEL,'apiKey':'keychain:'+account}}}
        (self.engine_home/'config.json').write_text(json.dumps(config),encoding='utf-8')
        self.payload=b''
        self.requests=0
        owner=self
        class Inner(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                owner.requests+=1
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200);self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(owner.payload)));self.end_headers()
                self.wfile.write(owner.payload)
        self.inner=ThreadingHTTPServer(('127.0.0.2',0),Inner)
        self.front=make_front(self.store,TOKEN,self.inner.server_port,self.engine_home)
        self.threads=[threading.Thread(target=s.serve_forever,daemon=True) for s in (self.inner,self.front)]
        for thread in self.threads:thread.start()

    def tearDown(self):
        for server in (self.front,self.inner):server.shutdown();server.server_close()
        for thread in self.threads:thread.join()
        self.temp.cleanup()

    def request(self,text,*,model=None,tools=True,native_tool=False,split=4,input_text='synthetic fixture'):
        self.payload=synthetic_stream(text,split=split,native_tool=native_tool)
        body={'model':model or self.alias,'input':input_text,'stream':True}
        if tools:body['tools']=[{'type':'function','name':'read_fixture','parameters':{'type':'object','properties':{}}}]
        conn=http.client.HTTPConnection('127.0.0.1',self.front.server_port,timeout=5)
        try:
            conn.request('POST','/v1/responses',json.dumps(body),{'X-Provider-Hub-Token':TOKEN,'Content-Type':'application/json'})
            res=conn.getresponse();return res.status,res.read()
        finally:conn.close()

    def test_incomplete_internal_markup_is_not_reported_as_success(self):
        status,raw=self.request('<thinking>synthetic internal text\n'+MARKERS,split=2)
        self.assertEqual(status,200)
        self.assertIn(b'response.incomplete',raw)
        self.assertNotIn(b'response.completed',raw)
        self.assertNotIn(b'<thinking>',raw)
        self.assertNotIn(b'DSML',raw)
        self.assertEqual(self.requests,1)  # No silent replay after possible prior actions.

    def test_code_fence_and_unrelated_model_are_preserved(self):
        text='```xml\n<thinking>example\n'+MARKERS+'\n```'
        status,raw=self.request(text)
        self.assertEqual(status,200);self.assertIn(b'response.completed',raw)
        self.assertIn(b'DSML',raw)
        status,raw=self.request('<thinking>example\n'+MARKERS,model='native-alpha')
        self.assertEqual(status,200);self.assertIn(b'response.completed',raw);self.assertIn(b'DSML',raw)

    def test_plain_text_and_normal_native_tool_events_are_preserved(self):
        for text,native in [('普通回复',False),('普通回复',True)]:
            status,raw=self.request(text,native_tool=native)
            self.assertEqual(status,200);self.assertIn(b'response.completed',raw)
            if native:self.assertIn(b'function_call',raw)

    def test_without_tools_the_same_text_is_preserved(self):
        status,raw=self.request('<thinking>example\n'+MARKERS,tools=False)
        self.assertEqual(status,200);self.assertIn(b'response.completed',raw);self.assertIn(b'DSML',raw)

    def test_explicit_markup_question_is_preserved(self):
        text='<thinking>example\n'+MARKERS
        status,raw=self.request(text,input_text='请原样展示 DSML 标签作为示例')
        self.assertEqual(status,200);self.assertIn(b'response.completed',raw);self.assertIn(b'DSML',raw)

    def test_guard_is_scoped_to_the_chat_protocol_not_model_names(self):
        request={'input':'fixture','tools':[{'type':'function','name':'fixture'}]}
        self.assertTrue(_guard_dsml_for(request,{'protocol':'chat_completions','model':MODEL}))
        self.assertFalse(_guard_dsml_for(request,{'protocol':'responses','model':MODEL}))
        self.assertFalse(_guard_dsml_for(request,{'protocol':'anthropic_messages','model':MODEL}))
        self.assertTrue(_guard_dsml_for(request,{'protocol':'chat_completions','model':'another-model'}))

    def test_complete_literal_content_is_not_silently_modified(self):
        text='<thinking>example</thinking>\n'+MARKERS
        raw=b''.join(guard_incomplete_dsml(safe_responses_stream(io.BytesIO(synthetic_stream(text)))))
        self.assertIn(b'response.completed',raw)
        self.assertIn(b'DSML',raw)


class DsmlGuardBoundaryTests(unittest.TestCase):
    @staticmethod
    def guard(raw):
        return b''.join(guard_incomplete_dsml(safe_responses_stream(io.BytesIO(raw))))

    def test_same_incident_at_different_chunk_sizes(self):
        text='<thinking>synthetic sentinel'+'x'*5000+'\n'+MARKERS
        for size in (2,20000):
            with self.subTest(size=size):
                raw=self.guard(synthetic_stream(text,split=size))
                self.assertIn(b'response.incomplete',raw)
                self.assertNotIn(b'response.completed',raw)
                self.assertNotIn(b'synthetic sentinel',raw)

    def test_incident_with_incomplete_or_missing_terminal(self):
        text='<thinking>synthetic sentinel\n'+MARKERS
        prefix=synthetic_stream(text).rsplit(b'event: response.completed',1)[0]
        for suffix in (b'',frame('response.incomplete',response={'id':'synthetic-response','status':'incomplete','output':[]})):
            raw=self.guard(prefix+suffix)
            self.assertIn(b'response.incomplete',raw)
            self.assertNotIn(b'synthetic sentinel',raw)

    def test_tool_events_do_not_mask_incident(self):
        text='<thinking>synthetic sentinel\n'+MARKERS
        raw=self.guard(synthetic_stream(text,native_tool=True))
        self.assertIn(b'response.incomplete',raw)
        self.assertNotIn(b'response.completed',raw)
        self.assertNotIn(b'synthetic sentinel',raw)

    def test_tool_result_original_text_is_preserved(self):
        source='<thinking>synthetic example\n'+MARKERS
        request={'input':[{'type':'function_call_output','call_id':'call_fixture','output':source},
                          {'role':'user','content':'请原样输出 fixture.xml 的内容'}],
                 'tools':[{'type':'function','name':'read_fixture'}]}
        self.assertTrue(_guard_dsml_for(request,{'protocol':'chat_completions','model':MODEL}))
        stream=safe_responses_stream(io.BytesIO(synthetic_stream(source)))
        raw=b''.join(guard_incomplete_dsml(stream,request=request))
        self.assertIn(b'response.completed',raw)
        self.assertIn(b'DSML',raw)

    def test_unrelated_old_tool_result_does_not_disable_guard(self):
        request={'input':[{'type':'function_call_output','call_id':'call_fixture','output':'source mentions DSML'},
                          {'role':'user','content':'继续修改页面'}],
                 'tools':[{'type':'function','name':'read_fixture'}]}
        self.assertTrue(_guard_dsml_for(request,{'protocol':'chat_completions','model':MODEL}))
        stream=safe_responses_stream(io.BytesIO(synthetic_stream('<thinking>synthetic sentinel\n'+MARKERS)))
        raw=b''.join(guard_incomplete_dsml(stream,request=request))
        self.assertIn(b'response.incomplete',raw)
        self.assertNotIn(b'synthetic sentinel',raw)

    def test_incomplete_marker_is_not_released_on_any_terminal(self):
        text='<thinking>synthetic sentinel\n</｜｜DSML｜｜ inv'
        prefix=synthetic_stream(text).rsplit(b'event: response.completed',1)[0]
        for suffix in (b'',frame('response.completed',response={'id':'fixture','status':'completed','output':[]}),
                       frame('response.incomplete',response={'id':'fixture','status':'incomplete','output':[]})):
            raw=self.guard(prefix+suffix)
            self.assertIn(b'response.incomplete',raw)
            self.assertNotIn(b'response.completed',raw)
            self.assertNotIn(b'synthetic sentinel',raw)

    def test_checks_each_message_in_mixed_tool_response(self):
        bad='<thinking>synthetic sentinel\n'+MARKERS
        for first,second in (('正常内容',bad),(bad,'正常内容')):
            chunks=[frame('response.created',response={'id':'fixture','status':'in_progress','output':[]})]
            for idx,text in enumerate((first,second)):
                item={'id':'msg_'+str(idx),'type':'message','role':'assistant','status':'in_progress','content':[]}
                chunks.extend([frame('response.output_item.added',output_index=idx,item=item),
                    frame('response.output_text.delta',item_id=item['id'],output_index=idx,content_index=0,delta=text),
                    frame('response.output_text.done',item_id=item['id'],output_index=idx,content_index=0,text=text),
                    frame('response.output_item.done',output_index=idx,item=dict(item,status='completed',
                         content=[{'type':'output_text','text':text,'annotations':[]}]))])
                if idx==0:
                    tool={'id':'fc_fixture','type':'function_call','name':'read_fixture','call_id':'call_fixture',
                          'arguments':'{}','status':'completed'}
                    chunks.append(frame('response.output_item.done',output_index=2,item=tool))
            chunks.append(frame('response.completed',response={'id':'fixture','status':'completed','output':[]}))
            raw=self.guard(b''.join(chunks))
            self.assertIn(b'response.incomplete',raw)
            self.assertNotIn(b'response.completed',raw)
            self.assertNotIn(b'synthetic sentinel',raw)

    def test_heartbeat_passes_while_candidate_is_buffered(self):
        chunks=[frame('response.created',response={'id':'fixture','status':'in_progress','output':[]}),
                frame('response.output_item.added',item={'id':'msg','type':'message'}),
                frame('response.output_text.delta',item_id='msg',delta='<thinking>'),
                frame('response.heartbeat'),
                frame('response.output_text.delta',item_id='msg',delta='example</thinking>'),
                frame('response.completed',response={'id':'fixture','status':'completed','output':[]})]
        out=guard_incomplete_dsml(safe_responses_stream(io.BytesIO(b''.join(chunks))))
        self.assertIn(b'response.created',next(out));self.assertIn(b'response.output_item.added',next(out))
        self.assertIn(b'response.heartbeat',next(out))
        self.assertIn(b'response.completed',b''.join(out))

    def test_legal_large_literal_has_same_result_at_different_chunk_sizes(self):
        text='<thinking>'+'x'*65536+'</thinking>'
        for size in (1,20000):
            with self.subTest(size=size):
                raw=self.guard(synthetic_stream(text,split=size))
                self.assertIn(b'response.completed',raw)
                self.assertNotIn(b'response.incomplete',raw)

    def test_oversized_suspect_candidate_fails_instead_of_leaking(self):
        text='<thinking>'+'x'*(128*1024+1)
        raw=self.guard(synthetic_stream(text,split=1000))
        self.assertIn(b'response.incomplete',raw)
        self.assertNotIn(b'response.completed',raw)
        self.assertNotIn(b'xxxxxxxxxxxxxxxx',raw)


if __name__ == '__main__':unittest.main()
