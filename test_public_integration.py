"""Integrate public references with a temporary, synthetic model connection."""
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import mcp_server
import public_evaluations
from storage import ProfileStore


class PublicReferenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ProfileStore(Path(self.temp.name))
        self.profile = self.store.save('私有线路名称','chat_completions','https://fixture.invalid/v1',
                                       'fixture-model','synthetic-key')
        self.alias = 'hub-' + self.profile['id'].replace('-','') + '/fixture-model'
        selected, _ = self.store.resolve_profile(self.profile['id'])
        account = 'hub-' + self.profile['id'].replace('-','') + '/22222222-2222-4222-8222-222222222222'
        self.store.bind_engine_route(self.alias, self.profile['id'], selected['_revision'], account)

    def test_scheduling_overview_fetches_only_public_model_name_and_caches_result(self):
        repo = 'publisher/fixture-model'
        urls = []
        def fake_http(url):
            urls.append(url)
            if '?search=' in url:
                return [{'id':repo}]
            return {'id':repo, 'evalResults':[{'verified':False,'data':{
                'dataset':{'id':'datacurve/deep-swe','task_id':'deep_swe'},'value':77.0,
                'date':'2026-10-17','source':{'name':'model card','url':'https://huggingface.co/'+repo}}}]}
        snapshot={'ok':True,'candidates':[{'model':self.alias,'state':'available'}], 'effective':{},'recent':[]}
        with patch.dict(os.environ, {'HF_HUB_OFFLINE':''}), \
             patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_scheduling', side_effect=lambda *a,**kw:dict(snapshot)), \
             patch.object(mcp_server, '_guard_status', return_value={}), \
             patch.object(public_evaluations, '_http_json', side_effect=fake_http):
            first = mcp_server.get_scheduling_overview()
            self.assertTrue(first['public_reference_pending'])
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                refresh=mcp_server.get_public_model_references()
                if not refresh['pending']: break
                time.sleep(.03)
            ref=refresh['references'][self.alias]
            self.assertEqual(ref['status'],'available')
            self.assertEqual(ref['results'][0]['score'],77.0)
            self.assertFalse(ref['results'][0]['verified'])
            second = mcp_server.get_scheduling_overview()
            self.assertEqual(second['candidates'][0]['public_reference']['repository'],repo)
            self.assertEqual(len(urls),2)
            self.assertTrue(all('synthetic-key' not in url and 'fixture.invalid' not in url
                                and '私有线路名称' not in url for url in urls))

    def test_recommendation_includes_public_reference_without_changing_choice(self):
        result={'ok':True,'model':self.alias,'effort':'high'}
        ref={'status':'available','results':[{'benchmark':'datacurve/deep-swe','score':77,'verified':False}]}
        with patch.object(mcp_server, '_recommend_subagent_model', return_value=result.copy()), \
             patch.object(mcp_server, 'ProfileStore', return_value=self.store), \
             patch.object(mcp_server, '_public_route_references', return_value=({self.alias:ref},False)):
            value=mcp_server.recommend_subagent_model('critical')
        self.assertEqual(value['model'],self.alias)
        self.assertEqual(value['public_reference'],ref)
        self.assertFalse(value['public_reference_pending'])

    def test_offline_or_unsupported_names_do_not_block_other_references(self):
        with patch.dict(os.environ, {'HF_HUB_OFFLINE':'1'}), \
             patch.object(mcp_server, 'read_references') as lookup:
            refs,pending=mcp_server._public_route_references(self.store)
        self.assertEqual((refs,pending),({},False))
        lookup.assert_not_called()
        with patch.dict(os.environ, {'HF_HUB_OFFLINE':''}), \
             patch.object(mcp_server, 'read_references', side_effect=ValueError('invalid identifier')):
            refs,pending=mcp_server._public_route_references(self.store)
        self.assertEqual(refs[self.alias]['status'],'missing')
        self.assertFalse(pending)


if __name__ == '__main__': unittest.main()
