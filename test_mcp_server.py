import json
import asyncio
import uuid
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent
FIXTURE_PROXY = {'HTTPS_PROXY':'http://127.0.0.1:9', 'https_proxy':'http://127.0.0.1:9',
                 'NO_PROXY':'127.0.0.1,localhost', 'no_proxy':'127.0.0.1,localhost'}


class FixtureProvider(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.headers.get('Authorization')=='Bearer wrong-key':
            self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
        metadata={'id':'fixture-model','context_length':128000,
                  'reasoning_efforts':['medium','high'],'default_reasoning_effort':'high'}
        body=json.dumps({'data':[metadata,{'id':'fixture-other'}]} if self.path.endswith('/models') else metadata).encode()
        self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if body.get('model') == 'invalid-model':
            self.send_response(401);self.send_header('Content-Length','0');self.end_headers();return
        if self.path.endswith('/systemone'):
            data=b'{"code":0,"data":{"answers":{"check":{"type":"noul","noul":0.9}}}}'
            mime='application/json'
        else:
            chunk={'object':'chat.completion.chunk','created':1,'choices':[{'index':0,
                   'delta':{'content':'PROVIDER_HUB_PROBE_OK'},'finish_reason':'stop','matched_stop':None}]}
            data=('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode()
            mime='text/event-stream'
        self.send_response(200);self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


def content(result):
    if result.is_error:
        raise AssertionError("MCP call failed")
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(next(item.text for item in result.content if item.type == "text"))


class McpServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_phase_job_rpc_with_lost_responses(self):
        fake=ThreadingHTTPServer(('127.0.0.1',0),FixtureProvider)
        worker=threading.Thread(target=fake.serve_forever,daemon=True);worker.start()
        self.addCleanup(fake.server_close);self.addCleanup(worker.join);self.addCleanup(fake.shutdown)
        with tempfile.TemporaryDirectory(prefix='provider-job-rpc-') as temp:
            params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp_server.py')],
                env=dict(os.environ,LOCALAPPDATA=temp,CODEX_HOME=str(Path(temp)/'codex-home'),**FIXTURE_PROXY),cwd=str(ROOT))
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    tools={t.name:t for t in (await session.list_tools()).tools}
                    for name in ('start_provider_save','get_provider_save_job','commit_provider_save_job','cancel_provider_save_job'):
                        self.assertEqual(tools[name].meta['ui']['visibility'],['app'])
                    self.assertIn('client_job_id',tools['start_provider_save'].input_schema['required'])
                    ident=str(uuid.uuid4())
                    args=dict(client_job_id=ident,name='RPC',protocol='system_one',base_url=f'http://127.0.0.1:{fake.server_port}/systemone',model='jev-a',models=['jev-a','jev-b'],key='synthetic-job-rpc-secret')
                    await session.call_tool('start_provider_save',args)  # Reply deliberately ignored.
                    for _ in range(100):
                        status=content(await session.call_tool('get_provider_save_job',{'job_id':ident}))
                        if status.get('state') in {'ready','failed'}:break
                        await asyncio.sleep(.02)
                    self.assertEqual(status['state'],'ready',status)
                    listed=content(await session.call_tool('list_providers',{}))
                    self.assertEqual(listed['profiles'],[])
                    self.assertEqual(content(await session.call_tool('start_provider_save',args))['state'],'ready')
                    await session.call_tool('commit_provider_save_job',{'job_id':ident})
                    for _ in range(100):
                        status=content(await session.call_tool('get_provider_save_job',{'job_id':ident}))
                        if status.get('state') in {'succeeded','failed','committed_cleanup_required'}:break
                        await asyncio.sleep(.02)
                    self.assertEqual(status['state'],'succeeded',status)
                    self.assertEqual(content(await session.call_tool('commit_provider_save_job',{'job_id':ident}))['state'],'succeeded')
                    self.assertEqual(content(await session.call_tool('start_provider_save',args))['state'],'succeeded')
                    self.assertNotIn('synthetic-job-rpc-secret',json.dumps(status))

    async def test_app_only_secret_tool_and_local_encrypted_store(self):
        marker = "synthetic-private-key-3ca4"
        fake=ThreadingHTTPServer(('127.0.0.1',0),FixtureProvider)
        worker=threading.Thread(target=fake.serve_forever,daemon=True);worker.start()
        self.addCleanup(fake.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(fake.shutdown)
        base=f'http://127.0.0.1:{fake.server_port}'
        with tempfile.TemporaryDirectory(prefix="provider-mcp-test-") as temp:
            params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")],
                                           env=dict(os.environ, LOCALAPPDATA=temp, CODEX_HOME=str(Path(temp)/"codex-home"), **FIXTURE_PROXY), cwd=str(ROOT))
            async with stdio_client(params) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
                    self.assertEqual(tools["save_provider"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["fetch_models"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["activate_connection"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["enable_codex"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["delete_provider"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["disable_connection"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["retry_credential_cleanup"].meta["ui"]["visibility"], ["app"])
                    self.assertEqual(tools["open_provider_manager"].meta["ui"]["resourceUri"],
                                     "ui://codex-provider-hub/manager.html")
                    self.assertEqual(tools["open_provider_manager"].meta["openai/ui"]["entrypoints"],
                                     [{"type": "global"}, {"type": "thread"}])
                    opened = content(await session.call_tool("open_provider_manager", {}))
                    self.assertEqual(opened["profiles"], [])
                    found = content(await session.call_tool("fetch_models", {
                        'protocol':'chat_completions','base_url':base+'/v1','key':marker}))
                    self.assertTrue(found['ok'])
                    self.assertEqual([x['id'] for x in found['models']],['fixture-model','fixture-other'])
                    self.assertNotIn(marker,json.dumps(found))
                    invalid = content(await session.call_tool("fetch_models", {
                        'protocol':'chat_completions','base_url':base+'/v1','key':'wrong-key'}))
                    self.assertFalse(invalid['ok']);self.assertNotIn('wrong-key',json.dumps(invalid))
                    result = content(await session.call_tool("save_provider", {
                        "name": "Fixture", "protocol": "chat_completions", "base_url": base + "/v1",
                        "model": "fixture-model", "key": marker,
                    }))
                    self.assertTrue(result["ok"])
                    self.assertNotIn(marker, json.dumps(result))
                    listing = content(await session.call_tool("list_providers", {}))
                    self.assertNotIn(marker, json.dumps(listing))
                    self.assertTrue(listing["profiles"][0]["has_key"])
                    self.assertEqual(listing["profiles"][0]["context_window"],128000)
                    self.assertEqual(listing["profiles"][0]["reasoning_levels"],['medium','high'])
                    self.assertEqual(result["detected"]["context_window"], 128000)
                    self.assertEqual(result["detected"]["reasoning_levels"], ['medium','high'])
                    found = content(await session.call_tool("fetch_models", {
                        'protocol':'chat_completions','base_url':base+'/v1','provider_id':result['profile']['id']}))
                    self.assertTrue(found['ok'])
                    failed = content(await session.call_tool("save_provider", {
                        "name":"Invalid", "protocol":"chat_completions", "base_url":base+"/v1",
                        "model":"invalid-model", "key":marker}))
                    self.assertFalse(failed['ok'])
                    self.assertEqual(failed['detail'],'upstream_rejected')
                    self.assertEqual(failed['http_status'],401)
                    self.assertIn('认证',failed['message'])
                    self.assertEqual(len(content(await session.call_tool("list_providers", {}))['profiles']),1)
                    removed = content(await session.call_tool("delete_provider", {"provider_id": result["profile"]["id"]}))
                    self.assertTrue(removed["ok"])
                    jev = content(await session.call_tool("save_provider", {
                        "name": "Jev", "protocol": "system_one", "base_url": base + "/v1/systemone",
                        "model": "fixture-jev", "key": marker,
                    }))
                    ident = jev["profile"]["id"]
                    self.assertTrue(content(await session.call_tool("activate_connection", {"provider_id": ident}))["ok"])
                    staged = content(await session.call_tool("save_provider", {
                        "name": "Jev changed", "protocol": "system_one", "base_url": base + "/v2/systemone",
                        "model": "fixture-jev", "key": "", "provider_id": ident,
                    }))
                    self.assertTrue(staged["ok"])
                    self.assertEqual(staged["profile"]["id"], ident)
                    self.assertEqual(content(await session.call_tool("list_providers", {}))["active_jev"], ident)
                    self.assertTrue(content(await session.call_tool("disable_connection", {"provider_id": ident}))["ok"])
                    listed = content(await session.call_tool("list_providers", {}))
                    self.assertIsNone(listed["active_jev"])
                    self.assertTrue(next(x for x in listed["profiles"] if x["id"] == ident)["disabled"])
                    self.assertTrue(content(await session.call_tool("activate_connection", {"provider_id": ident}))["ok"])
                    denied = content(await session.call_tool("delete_provider", {"provider_id": ident}))
                    self.assertEqual(denied["code"], "connection_in_use")
                    deleted = content(await session.call_tool("delete_provider", {"provider_id": ident, "confirm_active": True}))
                    self.assertTrue(deleted["ok"])
                    html = await session.read_resource("ui://codex-provider-hub/manager.html")
                    self.assertIn("管理模型连接", html.contents[0].text)
            self.assertNotIn(marker.encode(), (Path(temp) / "CodexProviderHub" / "profiles.sqlite").read_bytes())


if __name__ == "__main__":
    unittest.main()
