"""Native and managed identities are derived from local data, not model names."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from native_catalog import native_model_ids

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('neutral_catalog_reader', ROOT/'scripts'/'read-model-catalog.py')
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class NeutralCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='neutral-catalog-')
        self.root = Path(self.temp.name).resolve()
        if self.root.parent != Path(tempfile.gettempdir()).resolve():
            raise ValueError('unsafe test directory')
        self.home = self.root/'home'; self.home.mkdir()
        self.local = self.root/'local'; self.local.mkdir()
        self.hub = self.local/'CodexProviderHub'; self.hub.mkdir()
        self.patch = patch.dict(os.environ, {'LOCALAPPDATA':str(self.local),'CODEX_HOME':str(self.home)})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.temp.cleanup)

    def write(self, path, value): path.write_text(json.dumps(value),encoding='utf8')

    def test_cache_and_explicit_local_record_are_identities_not_name_patterns(self):
        self.write(self.home/'models_cache.json',{'models':[
            {'slug':'host/native-a','visibility':'list'}, {'slug':'hidden-host','visibility':'hide'}]})
        self.write(self.hub/'baseline-catalog.json',{'models':[
            {'slug':'selected-route','visibility':'list','comp_hash':'host-compatibility'},
            {'slug':'orphan/old-route','visibility':'list','comp_hash':None}]})
        (self.home/'config.toml').write_text('model="selected-route"\n',encoding='utf8')
        self.assertEqual(native_model_ids(self.home,self.hub),{'host/native-a'})
        self.write(self.hub/'native-models.json',{'schemaVersion':1,'models':['host/native-b','host/native-c']})
        self.assertEqual(native_model_ids(self.home,self.hub),{'host/native-a','host/native-b','host/native-c'})
        self.assertEqual(native_model_ids(self.home,self.hub,excluded_aliases={'host/native-b'}),{'host/native-a','host/native-c'})

    def test_invalid_local_identity_record_does_not_create_routes(self):
        self.write(self.hub/'native-models.json',{'schemaVersion':1,'models':['native-a',None]})
        self.assertEqual(native_model_ids(self.home,self.hub),set())

    def test_reader_keeps_valid_binding_and_drops_unowned_or_hidden_rows(self):
        self.write(self.home/'models_cache.json',{'models':[{'slug':'host/native-a','visibility':'list'}]})
        self.write(self.hub/'baseline-catalog.json',{'models':[{'slug':'host/native-a','visibility':'list'}]})
        rows=[{'slug':name,'visibility':visibility,'supported_reasoning_levels':[{'effort':'high'}]} for name,visibility in
              [('host/native-a','list'),('route/model','list'),('orphan/route','list'),('hidden','hide')]]
        self.write(self.hub/'catalog.json',{'models':rows})
        (self.home/'config.toml').write_text('model_catalog_json='+json.dumps(str(self.hub/'catalog.json'))+'\n',encoding='utf8')
        bindings={'route/model':{'revision':'a'*64,'model':'model','connection':'fixture'}}
        with patch.object(reader,'managed_models',return_value=(bindings,set(),{'route/model','orphan/route'})):
            result=reader.read_catalog(self.home)
        self.assertEqual(set(result['models']),{'host/native-a','route/model'})
        self.assertEqual(result['nativeModels'],['host/native-a'])
        self.assertEqual(set(result['hubModels']),{'route/model'})


if __name__ == '__main__': unittest.main()
