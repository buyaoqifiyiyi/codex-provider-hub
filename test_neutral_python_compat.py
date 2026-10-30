"""Shared isolated native-catalog fixtures for the migrated Python regressions."""
import json
import os
import ipaddress
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

NATIVE_MODELS = ('native-alpha', 'native-beta')


def isolated_native_catalog(test, root):
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect
    def local_resolve(host, *args, **kwargs):
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise socket.gaierror('External DNS disabled by test fixture')
        return original_resolve(host, *args, **kwargs)
    def local_connect(sock, address):
        if not ipaddress.ip_address(address[0]).is_loopback:
            raise AssertionError('External network disabled by test fixture')
        return original_connect(sock, address)
    for target, replacement in (('socket.getaddrinfo', local_resolve), ('socket.socket.connect', local_connect)):
        guard = patch(target, replacement)
        guard.start()
        test.addCleanup(guard.stop)
    home = Path(root) / 'native-home'
    home.mkdir(exist_ok=True)
    (home / 'models_cache.json').write_text(json.dumps({'models': [
        {'slug': model, 'visibility': 'list'} for model in NATIVE_MODELS]}), encoding='utf-8')
    (home / 'config.toml').write_text('model = "native-alpha"\n', encoding='utf-8')
    env = patch.dict(os.environ, {'CODEX_HOME': str(home), 'LOCALAPPDATA': str(Path(root) / 'local')})
    env.start()
    test.addCleanup(env.stop)
    return home


def alias_for(store, provider_id, model='fixture-beta'):
    from engine_control import public_aliases
    return public_aliases(store, provider_id, store.model_ids(provider_id))[model]


class NeutralPythonCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='neutral-python-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.native_home = isolated_native_catalog(self, self.root)

    def test_native_identity_comes_from_catalog_and_excludes_bound_aliases(self):
        from native_catalog import native_model_ids
        rows = [{'slug':'native-alpha'}, {'slug':'hidden-model','visibility':'hide'},
                {'slug':'connection/alias'}]
        (self.native_home/'models_cache.json').write_text(json.dumps({'models':rows}),encoding='utf-8')
        (self.native_home/'config.toml').write_text('model="selected-model"',encoding='utf-8')
        (self.root/'native-models.json').write_text(json.dumps({'schemaVersion':1,'models':['retained-native']}),encoding='utf-8')
        self.assertEqual(native_model_ids(self.native_home,self.root,['connection/alias']),
                         {'native-alpha','retained-native'})

    def test_readiness_keeps_native_and_arbitrary_bound_routes_independent(self):
        from storage import ProfileStore, OFFICIAL_CODEX_URL
        from front_proxy import read_engine_routes
        store=ProfileStore(self.root/'store')
        p=store.save('Synthetic','responses','https://fixture.invalid/v1','unfamiliar/model','fake-key')
        snapshot,_=store.resolve_profile(p['id'])
        account='arbitrary-route/12345678-1234-1234-1234-123456789abc'
        alias='unusual-public-name/unfamiliar/model'
        store.bind_engine_route(alias,p['id'],snapshot['_revision'],account)
        home=self.root/'engine';home.mkdir()
        config={'defaultProvider':'openai','providers':{'openai':{
            'adapter':'openai-responses','baseUrl':OFFICIAL_CODEX_URL,
            'authMode':'forward','codexAccountMode':'direct'},'arbitrary-route':{
            'adapter':'openai-responses','baseUrl':p['base_url'],'defaultModel':p['model'],
            'models':[p['model']],'apiKey':'keychain:'+account}}}
        def ready():
            (home/'config.json').write_text(json.dumps(config),encoding='utf-8')
            return read_engine_routes(store,home)
        self.assertEqual(ready(),(True,True))
        config['providers']['openai']['disabled']=True
        self.assertEqual(ready(),(False,True))
        config['providers']['arbitrary-route']['apiKey']='keychain:wrong-reference'
        self.assertEqual(ready(),(False,False))
        config['providers']['openai']['disabled']=False
        self.assertEqual(ready(),(True,False))
        config['providers']['arbitrary-route']['apiKey']='keychain:'+account
        self.assertEqual(ready(),(True,True))
        store.save('Changed','responses','https://changed.invalid/v1',p['model'],'new-fake-key',p['id'])
        self.assertEqual(ready(),(True,False))
