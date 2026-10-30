"""Synthetic end-to-end coverage of automatic model admission and disabling."""
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
IDENT='11111111-1111-4111-8111-111111111111'
MODEL='vendor/example-model'
ALIAS='hub-11111111111141118111111111111111/'+MODEL
ACCOUNT='hub-1111111111111111-111111111111/22222222-2222-4222-8222-222222222222'


def content(result):
    if result.is_error: raise AssertionError('MCP call failed')
    return result.structured_content or json.loads(next(c.text for c in result.content if c.type=='text'))


class DynamicSchedulingMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_model_appears_and_stays_unknown_when_disabled_or_reenabled(self):
        with tempfile.TemporaryDirectory(prefix='hub-dynamic-mcp-') as directory:
            local=Path(directory); home=local/'codex'; home.mkdir()
            (home/'models_cache.json').write_text(json.dumps({'models':[]}),encoding='utf-8')
            catalog=home/'catalog.json'
            (home/'config.toml').write_text('model_catalog_json='+json.dumps(str(catalog)),encoding='utf-8')
            catalog.write_text(json.dumps({'models':[
                {'slug':ALIAS,'supported_reasoning_levels':[{'effort':'medium'},{'effort':'high'}]},
                {'slug':'hub-unbound/other-model','supported_reasoning_levels':[{'effort':'high'}]}
            ]}),encoding='utf-8')
            store=ProfileStore(local/'CodexProviderHub')
            with store.atomic() as db:
                db.execute('INSERT INTO providers(id,name,protocol,base_url,model,encrypted_key) VALUES (?,?,?,?,?,?)',
                           (IDENT,'Fixture provider','chat_completions','https://fixture.invalid/v1',MODEL,b'synthetic-ciphertext'))
                db.execute('INSERT INTO connection_models(provider_id,model,position) VALUES (?,?,0)',(IDENT,MODEL))
                row=db.execute('SELECT id,name,protocol,base_url,model,encrypted_key FROM providers WHERE id=?',(IDENT,)).fetchone()
                revision=ProfileStore._content_revision(db,row)
                db.execute('INSERT INTO model_routes(alias,provider_id,model) VALUES (?,?,?)',(ALIAS,IDENT,MODEL))
                db.execute('INSERT INTO engine_bindings(alias,provider_id,revision,account,model) VALUES (?,?,?,?,?)',
                           (ALIAS,IDENT,revision,ACCOUNT,MODEL))
            env=dict(os.environ,LOCALAPPDATA=str(local),CODEX_HOME=str(home),PLUGIN_DATA=str(local/'audit'),
                     HTTPS_PROXY='http://127.0.0.1:9',https_proxy='http://127.0.0.1:9',HF_HUB_OFFLINE='1')
            params=StdioServerParameters(command=sys.executable,args=[str(ROOT/'mcp_server.py')],env=env,cwd=str(ROOT))
            async with stdio_client(params) as (reader,writer):
                async with ClientSession(reader,writer) as session:
                    await session.initialize()
                    snapshot=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertTrue(snapshot['ok'],snapshot)
                    self.assertEqual(snapshot['routing_mode'],'auto')
                    self.assertFalse(snapshot['preferences_saved'])
                    manual=content(await session.call_tool('save_scheduling_preferences',{'mode':'manual'}))
                    self.assertFalse(manual['ok'])
                    self.assertEqual(content(await session.call_tool('get_scheduling_overview',{}))['routing_mode'],'auto')
                    item=next(c for c in snapshot['candidates'] if c['model']==ALIAS)
                    self.assertEqual(item['state'],'available'); self.assertEqual(item['capability'],'unknown')
                    self.assertEqual(item['display_name'],MODEL)
                    self.assertTrue(item['critical_eligible'])
                    self.assertEqual(snapshot['effective']['bounded']['model'],ALIAS)
                    self.assertEqual(snapshot['effective']['critical']['model'],ALIAS)
                    self.assertNotIn('synthetic-ciphertext',json.dumps(snapshot))
                    catalog.write_text(json.dumps({'models':[
                        {'slug':ALIAS,'supported_reasoning_levels':[]}
                    ]}),encoding='utf-8')
                    missing=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(next(c for c in missing['candidates'] if c['model']==ALIAS)['state'],'effort_unknown')
                    self.assertFalse(missing['effective']['bounded']['ok'])
                    catalog.write_text(json.dumps({'models':[
                        {'slug':ALIAS,'supported_reasoning_levels':[{'effort':'medium'},{'effort':'high'}]}
                    ]}),encoding='utf-8')
                    prefs={task:{'model':ALIAS,'effort':'high','fallback_model':ALIAS,'fallback_effort':'high'}
                           for task in ('bounded','boundary','critical')}
                    self.assertTrue(content(await session.call_tool('save_scheduling_preferences',{'preferences':prefs,'mode':'manual'}))['ok'])
                    selected=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(selected['routing_mode'],'manual')
                    self.assertTrue(selected['preferences_saved'])
                    self.assertEqual(selected['effective']['critical']['model'],ALIAS)
                    with store.atomic() as db: db.execute('INSERT INTO disabled_connections(provider_id) VALUES (?)',(IDENT,))
                    paused=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertTrue(paused['ok'],paused)
                    self.assertEqual(next(c for c in paused['candidates'] if c['model']==ALIAS)['state'],'disabled')
                    self.assertFalse(paused['effective']['bounded']['ok'])
                    self.assertFalse(paused['effective']['critical']['ok'])
                    self.assertEqual(paused['preferences']['bounded']['model'],ALIAS)
                    with store.atomic() as db: db.execute('DELETE FROM disabled_connections WHERE provider_id=?',(IDENT,))
                    enabled=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(enabled['effective']['bounded']['model'],ALIAS)
                    self.assertEqual(enabled['effective']['critical']['model'],ALIAS)
                    self.assertEqual(enabled['preferences']['bounded']['model'],ALIAS)
                    self.assertEqual(content(await session.call_tool('save_scheduling_preferences',{'mode':'auto'}))['routing_mode'],'auto')
                    automatic=content(await session.call_tool('get_scheduling_overview',{}))
                    self.assertEqual(automatic['routing_mode'],'auto')
                    self.assertEqual(automatic['preferences']['bounded']['model'],ALIAS)


if __name__ == '__main__':unittest.main()
