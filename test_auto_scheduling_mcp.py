"""Automatic selection through the real MCP with only local synthetic evidence."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from storage import ProfileStore

ROOT=Path(__file__).resolve().parent
IDS=['11111111-1111-4111-8111-111111111111','22222222-2222-4222-8222-222222222222']
MODELS=['model-a','model-b']
ALIASES=['hub-'+ident.replace('-','')+'/'+model for ident,model in zip(IDS,MODELS)]

def content(result):
    if result.is_error: raise AssertionError('MCP call failed')
    return result.structured_content or json.loads(next(c.text for c in result.content if c.type=='text'))


class AutoSchedulingMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_uses_matching_public_evidence_and_preserves_manual_settings(self):
        with tempfile.TemporaryDirectory(prefix='hub-auto-mcp-') as directory:
            local=Path(directory);root=local/'CodexProviderHub';root.mkdir();home=local/'codex';home.mkdir()
            store=ProfileStore(root)
            with store.atomic() as db:
                for ident,model,alias in zip(IDS,MODELS,ALIASES):
                    db.execute('INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?)',
                               (ident,'Fixture '+model,'chat_completions','https://fixture.invalid/v1',model,b'synthetic-ciphertext'))
                    db.execute('INSERT INTO connection_models(provider_id,model,position) VALUES (?,?,0)',(ident,model))
                    row=db.execute('SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?',(ident,)).fetchone()
                    revision=ProfileStore._content_revision(db,row)
                    db.execute('INSERT INTO model_routes(alias,provider_id,model) VALUES (?,?,?)',(alias,ident,model))
                    account='hub-'+ident.replace('-','')[:16]+'-111111111111/33333333-3333-4333-8333-333333333333'
                    db.execute('INSERT INTO engine_bindings(alias,provider_id,revision,account,model) VALUES (?,?,?,?,?)',
                               (alias,ident,revision,account,model))
            (home/'catalog.json').write_text(json.dumps({'models':[
                {'slug':alias,'supported_reasoning_levels':[{'effort':'high'}]} for alias in ALIASES]}),encoding='utf8')
            (home/'models_cache.json').write_text(json.dumps({'models':[]}),encoding='utf8')
            (home/'config.toml').write_text('model_catalog_json = '+json.dumps((home/'catalog.json').as_posix())+'\n',encoding='utf8')
            prefs={tier:{'model':ALIASES[0],'effort':'high','fallback_model':ALIASES[1],'fallback_effort':'high'}
                   for tier in ('bounded','boundary','critical')}
            (root/'scheduling.json').write_text(json.dumps(prefs),encoding='utf8')
            from public_evaluations import _db, _load_sources, _source_signature, read_references
            with _db(root) as db:
                for model,score in zip(MODELS,(40,90)):
                    ref={'status':'available','model_id':model,'_source_signature':_source_signature(_load_sources(root),model),'results':[{'benchmark':'terminal-bench 2.1',
                         'task_id':'terminalbench_2_1','unit':'%','conditions_fingerprint':'a'*64,'score':score}]}
                    db.execute('INSERT INTO public_references(model,payload,checked,expires) VALUES (?,?,?,?)',
                               (model,json.dumps(ref),time.time(),time.time()+3600))
            self.assertIsNotNone(store.get_engine_binding(ALIASES[0]))
            env=dict(os.environ,LOCALAPPDATA=str(local),CODEX_HOME=str(home),PLUGIN_DATA=str(local/'audit'),
                     HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9')
            # Fresh, source-matched synthetic cache entries need no refresh.
            self.assertFalse(read_references(root,MODELS)[1])
            env.pop('HF_HUB_OFFLINE',None)
            params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp_server.py')],env=env,cwd=str(local))
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    snapshot=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(snapshot['routing_mode'],'auto')
                    self.assertTrue(snapshot['effective']['bounded']['ok'],snapshot)
                    self.assertEqual(snapshot['effective']['bounded']['model'],ALIASES[1])
                    self.assertEqual(snapshot['preferences']['bounded']['model'],ALIASES[0])
                    result=content(await session.call_tool('recommend_subagent_model',{'task_type':'bounded','purpose':'text'}))
                    self.assertEqual(result['model'],ALIASES[1])
                    self.assertEqual(result['source'],'automatic')
                    self.assertEqual(result['routing_mode'],'auto')
                    explicit=content(await session.call_tool('recommend_subagent_model',
                        {'task_type':'bounded','purpose':'text','requested_model':ALIASES[0],'requested_effort':'high'}))
                    self.assertEqual(explicit['model'],ALIASES[0])
                    updated=content(await session.call_tool('save_scheduling_preferences',{'preferences':prefs}))
                    self.assertEqual(updated['routing_mode'],'auto')
                    self.assertEqual(content(await session.call_tool('get_scheduling_overview',{}))['routing_mode'],'auto')
                    self.assertEqual(content(await session.call_tool('save_scheduling_preferences',
                        {'preferences':prefs,'mode':'manual'}))['routing_mode'],'manual')
                    manual=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(manual['effective']['bounded']['model'],ALIASES[0])
                    self.assertEqual(content(await session.call_tool('save_scheduling_preferences',{'mode':'auto'}))['routing_mode'],'auto')
                    again=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(again['effective']['bounded']['model'],ALIASES[1])
                    self.assertEqual(again['preferences']['bounded']['model'],ALIASES[0])
                    self.assertNotIn('synthetic-ciphertext',json.dumps(again))


if __name__=='__main__':unittest.main()
