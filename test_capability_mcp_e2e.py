"""Exercise the installed-style MCP, local gateway and SQLite with synthetic data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ocx_service import ENGINE_VERSION
from storage import ProfileStore, _dpapi


class CapabilityMcpE2E(unittest.IsolatedAsyncioTestCase):
    async def test_tool_uses_synthetic_gateway_and_reuses_same_route_evidence(self):
        received = []
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                raw = b'{"capability_binding_guard":true,"capability_deadline_guard":true}'
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append({'body':data,'revision':self.headers.get('X-Provider-Hub-Expected-Revision'),
                                 'account':self.headers.get('X-Provider-Hub-Expected-Account')})
                message = {'type':'message','status':'completed','content':[{'type':'output_text',
                           'text':'{"left":"red","right":"blue"}'}]}
                frame = {'type':'response.completed','response':{'status':'completed','output':[message]}}
                raw = ('data: '+json.dumps(frame)+'\n\n').encode()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Content-Length',str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        gateway = ThreadingHTTPServer(('127.0.0.1',0), Gateway)
        worker = threading.Thread(target=gateway.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory(prefix='capability-mcp-e2e-') as tmp:
                local = Path(tmp)
                home = local/'codex'; home.mkdir()
                root = local/'CodexProviderHub'
                store = ProfileStore(root)
                profile = store.save('fixture', 'chat_completions', 'https://example.test/v1', 'image-fixture', 'synthetic-key')
                alias = 'hub-'+profile['id'].replace('-','')+'/image-fixture'
                current, _ = store.resolve_profile(profile['id'])
                account = 'hub-'+profile['id'].replace('-','')+'/22222222-2222-4222-8222-222222222222'
                store.bind_engine_route(alias, profile['id'], current['_revision'], account)
                (root/'gateway-state.json').write_text(json.dumps({'engine':ENGINE_VERSION,
                    'port':gateway.server_port,'inner_port':gateway.server_port}),encoding='utf-8')
                (root/'gateway-capability.bin').write_bytes(_dpapi(b'synthetic-token'))
                (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':alias,
                    'supported_reasoning_levels':[{'effort':'high'}]}]}),encoding='utf-8')
                env = dict(os.environ, LOCALAPPDATA=str(local), CODEX_HOME=str(home),
                           NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost',HF_HUB_OFFLINE='1')
                env.pop('CODEX_THREAD_ID',None); env.pop('CODEX_SESSION_ID',None)
                source = Path(__file__).resolve().parent
                params = StdioServerParameters(command=sys.executable,args=[str(source/'mcp_server.py')],
                                               cwd=str(source),env=env)
                async with stdio_client(params) as (reader,writer):
                    async with ClientSession(reader,writer) as session:
                        await session.initialize()
                        args={'task_type':'bounded','requested_model':alias,'requested_effort':'high','purpose':'image'}
                        first=await session.call_tool('recommend_subagent_model', args)
                        second=await session.call_tool('recommend_subagent_model', args)
                        self.assertFalse(first.is_error)
                        a=first.structured_content or json.loads(next(c.text for c in first.content if c.type=='text'))
                        b=second.structured_content or json.loads(next(c.text for c in second.content if c.type=='text'))
                        self.assertEqual(a['purpose_status'],'sample_passed',a)
                        self.assertTrue(b['capability_sample']['cached'])
                        self.assertEqual(a['dispatch_status'],'pending_dispatch')
                        self.assertEqual(len(received),1)
                        self.assertEqual(received[0]['body']['model'],alias)
                        self.assertEqual(received[0]['revision'],current['_revision'])
                        self.assertEqual(received[0]['account'],account)
                        self.assertNotIn('synthetic-key',json.dumps(a))
                evidence=store.capability_evidence(profile['id'],'image-fixture')
                self.assertTrue(any(row['purpose']=='image' and row['channel']=='subagent_gateway'
                                    and row['status']=='passed' for row in evidence))
        finally:
            gateway.shutdown(); gateway.server_close(); worker.join(5)


if __name__ == '__main__': unittest.main()
