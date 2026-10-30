'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {execFileSync,execSync,spawnSync}=require('node:child_process');
const {loadDisabledModels}=require('../lib/disabled-models.cjs');
const {runHook:executeHook}=require('../hooks/guard.cjs');
const {listModels}=require('../scripts/list-models.cjs');
const guard=require('../lib/guard.cjs');
const MODEL='legacy-fixture/model-a',ALIAS='another-route/model-a',NATIVE='native-fixture',OTHER='native-other';
const binding={revision:'a'.repeat(64),model:'upstream-model-a',connection:'bound-fixture'};
const policy={schemaVersion:2,allowedFamilies:[],allowedEffortsByFamily:{},jevAdvisory:{enabled:false}};
const catalog=()=>({source:'fixture',models:{[MODEL]:['high','max'],[NATIVE]:['medium'],[OTHER]:['high']},
 hubModels:{[MODEL]:binding},nativeModels:[NATIVE,OTHER]});
const auditRoot=makeTestDir(test,'hub-guard-audit-');
function runHook(envelope,dependencies={}) {
 return executeHook(envelope,{guard:{...guard,parentProvider:()=>null,senderContext:()=>({version:'v1'})},
  resolveBindingKind:async()=>({...binding,kind:'bound'}),resolveParentRoute:async()=> 'compatible',...dependencies,
  options:{dataDir:auditRoot,policy,...dependencies.options}});
}
function database(t,sql){
 const root=makeTestDir(t,'hub-disabled-'),dir=path.join(root,'CodexProviderHub');fs.mkdirSync(dir);
 const file=path.join(dir,'profiles.sqlite');
 if(sql!==undefined)execFileSync('python',['-B','-c','import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.executescript(sys.stdin.read()); db.close()',file],
  {input:sql,windowsHide:true,stdio:['pipe','pipe','pipe']});
 return {root,file};
}
const schema=`CREATE TABLE providers(id TEXT PRIMARY KEY,protocol TEXT,model TEXT);
CREATE TABLE model_routes(alias TEXT PRIMARY KEY,provider_id TEXT);
CREATE TABLE disabled_connections(provider_id TEXT PRIMARY KEY);
INSERT INTO providers VALUES ('bound-fixture','chat_completions','upstream-model-a'),
 ('native-disabled','codex_subscription','${NATIVE}'),('enabled','codex_subscription','${OTHER}');
INSERT INTO model_routes VALUES ('${MODEL}','bound-fixture'),('${ALIAS}','bound-fixture'),('enabled-alias','enabled');
INSERT INTO disabled_connections VALUES ('bound-fixture'),('native-disabled');`;
function never(){throw Error('forbidden route or network check');}
function input(model,tool='spawn_agent'){
 return {hook_event_name:'PreToolUse',tool_name:tool,tool_input:{model,reasoning_effort:model===NATIVE?'medium':'high'}};
}
test('read-only database lookup covers multiple aliases and native subscriptions',t=>{
 const {file}=database(t,schema),before=fs.readFileSync(file);
 assert.deepEqual([...loadDisabledModels({databasePath:file})].sort(),[MODEL,ALIAS,NATIVE].sort());assert.deepEqual(fs.readFileSync(file),before);
});
test('absent database and pre-disable schema remain compatible; corrupt metadata fails closed',t=>{
 const {file}=database(t);assert.equal(loadDisabledModels({databasePath:file}).size,0);assert.equal(fs.existsSync(file),false);
 execFileSync('python',['-B','-c','import sqlite3,sys; sqlite3.connect(sys.argv[1]).close()',file]);
 assert.equal(loadDisabledModels({databasePath:file}).size,0);fs.writeFileSync(file,'corrupt fixture');
 assert.throws(()=>loadDisabledModels({databasePath:file}),{code:'MODEL_DISABLE_STATE_UNAVAILABLE'});
 const broken=database(t,'CREATE TABLE disabled_connections(provider_id TEXT PRIMARY KEY);');
 assert.throws(()=>loadDisabledModels({databasePath:broken.file}),{code:'MODEL_DISABLE_STATE_UNAVAILABLE'});
});
test('every spawn name rejects a disabled binding before catalog, parent or route reads',async()=>{
 for(const tool of ['spawn_agent','Agent','collaborationspawn_agent','collaboration.spawn_agent','multi_agent_v1spawn_agent','multi_agent_v1__spawn_agent']){
  const output=await runHook(input(MODEL,tool),{loadDisabledModels:()=>new Set([MODEL]),loadAvailableModels:never,
   resolveBindingKind:never,resolveParentRoute:never,guard:{...guard,parentProvider:never,senderContext:never}});
  assert.match(output.hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED.*手动停用/);
 }
});
test('disabled native model is rejected while another native model stays eligible',async()=>{
 const deps={loadDisabledModels:()=>new Set([NATIVE,MODEL]),loadAvailableModels:catalog,resolveParentRoute:never};
 assert.match((await runHook(input(NATIVE),deps)).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED/);
 assert.equal(await runHook(input(OTHER),deps),null);
});
test('enabled binding checks stable identity and parent without provider probes',async()=>{
 const calls=[];
 const result=await runHook(input(MODEL),{loadDisabledModels:()=>{calls.push('disabled');return new Set();},
  loadAvailableModels:()=>{calls.push('catalog');return catalog();},resolveBindingKind:async()=>{calls.push('binding');return {...binding,kind:'bound'};},
  resolveParentRoute:async()=>{calls.push('parent');return 'compatible';}});
 assert.equal(result,null);assert.deepEqual(calls,['disabled','catalog','binding','parent','binding']);
});
test('candidate list excludes disabled models without checking any route',async()=>{
 const result=await listModels({loadDisabledModels:()=>new Set([MODEL,NATIVE]),loadAvailableModels:catalog,
  currentSession:never,resolveBindingKind:never,resolveParentRoute:never});
 assert.deepEqual(result.models,{[OTHER]:['high']});assert.equal(result.routeStatus,'pending_dispatch');
});
test('database-backed disabled decisions feed hook and listing, then re-enable immediately',async t=>{
 const {file}=database(t,schema);const deps={loadDisabledModels:()=>loadDisabledModels({databasePath:file}),loadAvailableModels:catalog,
  currentSession:never,resolveBindingKind:never,resolveParentRoute:never};
 for(const model of [MODEL,NATIVE])assert.match((await runHook(input(model),deps)).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED/);
 assert.deepEqual((await listModels(deps)).models,{[OTHER]:['high']});
 execFileSync('python',['-B','-c','import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute("DELETE FROM disabled_connections"); db.commit(); db.close()',file]);
 assert.equal(loadDisabledModels({databasePath:file}).size,0);
 assert.deepEqual((await listModels(deps)).models,catalog().models);
});
test('incompatible parent route denies the enabled binding',async()=>{
 const output=await runHook(input(MODEL),{loadDisabledModels:()=>new Set(),loadAvailableModels:catalog,resolveParentRoute:async()=> 'official'});
 assert.match(output.hookSpecificOutput.permissionDecisionReason,/HUB_PARENT_ROUTE_UNKNOWN/);
});
test('database failure stops hook and list before route checks and CLI denies conservatively',async t=>{
 const {file,root}=database(t);fs.writeFileSync(file,'corrupt fixture');
 const deps={loadDisabledModels:()=>loadDisabledModels({databasePath:file}),loadAvailableModels:never,resolveParentRoute:never};
 await assert.rejects(runHook(input(MODEL),deps),{code:'MODEL_DISABLE_STATE_UNAVAILABLE'});
 await assert.rejects(listModels(deps),{code:'MODEL_DISABLE_STATE_UNAVAILABLE'});
 const result=JSON.parse(execFileSync(process.execPath,[path.join(__dirname,'../hooks/guard.cjs')],{
  input:JSON.stringify(input(MODEL)),encoding:'utf8',windowsHide:true,env:{...process.env,LOCALAPPDATA:root},stdio:['pipe','pipe','pipe']}));
 assert.match(result.hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLE_STATE_UNAVAILABLE/);
});
test('registered hook commands consume stdin using isolated local state and never contact a service',t=>{
 const {root,file}=database(t,schema),pluginRoot=path.resolve(__dirname,'..'),codexDir=path.join(root,'codex');fs.mkdirSync(codexDir);
 fs.writeFileSync(path.join(codexDir,'models_cache.json'),JSON.stringify({models:[{slug:OTHER,supported_reasoning_levels:[{effort:'high'}]}]}));
 const preload=path.join(root,'no-network.cjs');
 fs.writeFileSync(preload,`
  const path=require('node:path');const root=process.env.PLUGIN_ROOT || process.env.CLAUDE_PLUGIN_ROOT;
  const forbidden=()=>{throw Error('FORBIDDEN_ROUTE_OR_NETWORK_CHECK');};
  require(path.join(root,'lib','parent-route.cjs')).resolveParentRoute=forbidden;
  require(path.join(root,'lib','binding-kind.cjs')).resolveBindingKind=forbidden;
  for(const name of ['node:http','node:https']){const m=require(name);m.request=forbidden;m.get=forbidden;}
  const net=require('node:net');net.connect=forbidden;net.createConnection=forbidden;
  require('node:tls').connect=forbidden;global.fetch=forbidden;
 `);
 const env={...process.env,PLUGIN_ROOT:pluginRoot,LOCALAPPDATA:root,CODEX_HOME:codexDir,PLUGIN_DATA:path.join(root,'audit'),
  NODE_OPTIONS:`--require="${preload.replaceAll('\\','/')}"`};
 const hooks=JSON.parse(fs.readFileSync(path.join(pluginRoot,'hooks/hooks.json'),'utf8')).hooks;
 function execute(hook,envelope,environment=env){return execSync(hook.command,{input:JSON.stringify(envelope),encoding:'utf8',
  env:environment,cwd:root,timeout:8000,windowsHide:true,stdio:['pipe','pipe','pipe']}).trim();}
 const spawnHook=hooks.PreToolUse.find(entry=>new RegExp(entry.matcher).test('spawn_agent')).hooks[0];
 for(const model of [MODEL,NATIVE])assert.match(JSON.parse(execute(spawnHook,input(model))).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED/);
 assert.equal(execute(spawnHook,input(OTHER)),'');
 const fallback={...env,CLAUDE_PLUGIN_ROOT:pluginRoot};delete fallback.PLUGIN_ROOT;
 assert.match(JSON.parse(execute(spawnHook,input(MODEL),fallback)).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED/);
 fs.writeFileSync(file,'corrupt fixture');
 assert.match(JSON.parse(execute(spawnHook,input(MODEL))).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLE_STATE_UNAVAILABLE/);
 for(const entries of Object.values(hooks))for(const entry of entries)for(const hook of entry.hooks){
  const result=spawnSync(hook.command,{shell:true,input:'{',encoding:'utf8',env,cwd:root,timeout:8000,windowsHide:true,stdio:['pipe','pipe','pipe']});
  assert.equal(result.status,0);assert.match(result.stderr,/malformed hook envelope/);
 }
});
