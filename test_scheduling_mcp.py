import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from storage import ProfileStore

ROOT=Path(__file__).resolve().parent
ROUTE='fixture-route/fixture-chat'
DEFAULTS={'bounded':{'model':ROUTE,'effort':'high','fallback_model':'native-alpha','fallback_effort':'medium'},
          'boundary':{'model':ROUTE,'effort':'max','fallback_model':'native-beta','fallback_effort':'high'},
          'critical':{'model':'native-beta','effort':'high','fallback_model':'native-gamma','fallback_effort':'high'}}


def content(result):
    if result.is_error: raise AssertionError('MCP call failed')
    return result.structured_content or json.loads(next(c.text for c in result.content if c.type=='text'))


class SchedulingMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_preferences_saved_and_used_in_real_mcp_with_disabled_route(self):
        with tempfile.TemporaryDirectory(prefix='hub-routing-mcp-') as tmp:
            local=Path(tmp); home=local/'codex'; home.mkdir()
            (home/'catalog.json').write_text(json.dumps({'models':[
                {'slug':'native-alpha','supported_reasoning_levels':[{'effort':'medium'}]},
                {'slug':'native-beta','supported_reasoning_levels':[{'effort':'high'}]},
                {'slug':'native-gamma','supported_reasoning_levels':[{'effort':'high'}]},
                {'slug':ROUTE,'supported_reasoning_levels':[{'effort':'high'},{'effort':'max'}]}
            ]}),encoding='utf-8')
            (home/'models_cache.json').write_text(json.dumps({'models':[{'slug':name,'supported_reasoning_levels':[{'effort':'high'}]} for name in ('native-alpha','native-beta','native-gamma')]}),encoding='utf8')
            (home/'config.toml').write_text('model_catalog_json = '+json.dumps((home/'catalog.json').as_posix())+'\n',encoding='utf8')
            store=ProfileStore(local/'CodexProviderHub')
            (local/'CodexProviderHub').mkdir(parents=True,exist_ok=True)
            (local/'CodexProviderHub'/'scheduling-mode.json').write_text('{"mode":"manual"}',encoding='utf8')
            (local/'CodexProviderHub'/'scheduling.json').write_text(json.dumps(DEFAULTS),encoding='utf8')
            with store.atomic() as db:
                db.execute('INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?)',
                           ('fixture','Fixture','chat_completions','https://example.test/v1','fixture-chat',b'synthetic-cipher'))
                db.execute('INSERT INTO connection_models(provider_id,model,position) VALUES (?,?,0)',('fixture','fixture-chat'))
                db.execute('INSERT INTO model_routes(alias,provider_id,model) VALUES (?,?,?)',(ROUTE,'fixture','fixture-chat'))
                db.execute('INSERT INTO disabled_connections(provider_id) VALUES (?)',('fixture',))
            env=dict(os.environ,LOCALAPPDATA=str(local),CODEX_HOME=str(home),PLUGIN_DATA=str(local/'audit'),HF_HUB_OFFLINE='1')
            params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp_server.py')],env=env,cwd=str(local))
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    tools={t.name:t for t in (await session.list_tools()).tools}
                    self.assertEqual(tools['save_scheduling_preferences'].meta['ui']['visibility'],['app'])
                    self.assertIn('recommend_subagent_model',tools)
                    snapshot=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertTrue(snapshot['ok'],snapshot)
                    self.assertEqual(snapshot['guard']['session_coverage'],'unknown')
                    self.assertEqual(snapshot['effective']['bounded']['model'],'native-alpha')
                    self.assertEqual(snapshot['effective']['bounded']['skipped'][0]['code'],'MODEL_DISABLED')
                    prefs=snapshot['preferences']; prefs['bounded']['model']='native-beta'; prefs['bounded']['effort']='high'
                    self.assertTrue(content(await session.call_tool('save_scheduling_preferences',{'preferences':prefs}))['ok'])
                    refreshed=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(refreshed['routing_mode'],'manual')
                    self.assertEqual(refreshed['effective']['bounded']['model'],'native-beta')
                    rec=content(await session.call_tool('recommend_subagent_model',{'task_type':'bounded'}))
                    self.assertEqual((rec['model'],rec['effort']),('native-beta','high'))
                    explicit=content(await session.call_tool('recommend_subagent_model',
                        {'task_type':'bounded','requested_model':ROUTE,'requested_effort':'high'}))
                    self.assertEqual(explicit['code'],'EXPLICIT_MODEL_UNAVAILABLE')
                    self.assertEqual(explicit['skipped'][0]['code'],'MODEL_DISABLED')
                    self.assertNotIn('synthetic-cipher',json.dumps(snapshot))


if __name__ == '__main__': unittest.main()
