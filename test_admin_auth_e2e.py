"""Verify old/new OpenCodex management tokens in an isolated, synthetic engine."""
import http.client,json,os,secrets,socket,subprocess,tempfile,time
import ocx_service
from engine_control import EngineManager
from pathlib import Path
ENGINE=Path(os.environ.get('LOCALAPPDATA', ''))/'CodexProviderHub'/'engine-bin'/'v2.93.0'/'ocx.exe'

def main():
    with tempfile.TemporaryDirectory(prefix='hub-admin-isolation-') as tmp:
     root=Path(tmp); home=root/'engine-state';home.mkdir()
     for name in ('user','local','appdata','codex'):(root/name).mkdir()
     with socket.socket() as s:s.bind(('127.0.0.2',0));port=s.getsockname()[1]
     old='ocx_admin_'+secrets.token_urlsafe(32);new=ocx_service._create_admin_capability(root)
     (home/'admin-api-token').write_text(old,encoding='ascii')
     config={'port':port,'hostname':'127.0.0.2','defaultProvider':'openai','syncResumeHistory':False,
             'clientIntegrations':{'codex':False,'grok':False,'claude-desktop':False},
             'claudeCode':{'enabled':False,'systemEnv':False},
             'providers':{'openai':{'adapter':'openai-responses','baseUrl':'https://chatgpt.com/backend-api/codex',
                                    'authMode':'forward','codexAccountMode':'direct'}}}
     (home/'config.json').write_text(json.dumps(config),encoding='utf8')
     env=ocx_service._engine_environment(root,'synthetic-admission-'+secrets.token_urlsafe(24))
     (root/'gateway-state.json').write_text(json.dumps({'inner_port':port}),encoding='utf8')
     row={'adapter':'openai-chat','baseUrl':'http://127.0.0.1:9/v1','allowPrivateNetwork':True,
          'authMode':'key','apiKey':'keychain:hub-test/12345678-1234-1234-1234-123456789abc',
          'models':['fixture'],'defaultModel':'fixture','liveModels':False,
          'modelContextWindows':{'fixture':32768},'modelAutoCompactTokenLimits':{'fixture':29491}}
     def status(token):
      c=http.client.HTTPConnection('127.0.0.2',port,timeout=8)
      try:
       c.request('POST','/api/providers',json.dumps({'name':'hub-test','provider':row}),
                 {'Content-Type':'application/json','Authorization':'Bearer '+token})
       r=c.getresponse();r.read();return r.status
      finally:c.close()
     def run():
      proc=subprocess.Popen([str(ENGINE),'start','--port',str(port)],env=env,cwd=root,stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
      try:
       for _ in range(300):
        if proc.poll() is not None:raise RuntimeError('fixture engine exited')
        try:
         c=http.client.HTTPConnection('127.0.0.2',port,timeout=.2);c.request('GET','/healthz');r=c.getresponse();r.read();c.close()
         if r.status==200:break
        except OSError:pass
        time.sleep(.1)
       else:raise RuntimeError('fixture engine timeout')
       denied,allowed=status(old),status(new)
       manager=EngineManager(root)
       assert manager.delete_provider('hub-test')
       assert 'hub-test' not in manager.snapshot()['providers']
       assert manager.replace_provider('hub-test',row)
       assert manager.snapshot()['providers']['hub-test']['defaultModel']=='fixture'
       assert ocx_service._read_admin_capability(root)==new
       assert new.encode() not in (root/'management-v2.bin').read_bytes()
       return denied,allowed
      finally:
       if proc.poll() is None:proc.terminate();proc.wait(timeout=8)
     first=run();second=run()
     result={'legacy_denied':all(pair[0] in (401,403) for pair in (first,second)),
             'new_allowed':all(pair[1]==200 for pair in (first,second)),
             'verified_after_restart':True,'manager_update_and_rollback':True,'dpapi_not_plaintext':True}
     print(json.dumps(result))
     if not all(result.values()):raise SystemExit(1)


if __name__ == "__main__":
    main()
