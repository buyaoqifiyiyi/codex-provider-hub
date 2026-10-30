'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),{execFileSync}=require('node:child_process');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const isolatedLocal=makeTestDir(test,'neutral-local-');
const previousLocal=process.env.LOCALAPPDATA;
process.env.LOCALAPPDATA=isolatedLocal;
test.after(()=>{
 if(previousLocal===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=previousLocal;
});
const {runHook}=require('../hooks/guard.cjs'),guard=require('../lib/guard.cjs');
const {listModels,verifyManaged,recommendFromSnapshot}=require('../scripts/list-models.cjs');
const {recoverTask}=require('../lib/v2-task-recovery.cjs');
const {recordAutoSelection}=require('../lib/auto-selection.cjs');
const policy={schemaVersion:2,allowedFamilies:[],allowedEffortsByFamily:{},jevAdvisory:{enabled:false}};
const ROOT=path.resolve(__dirname,'..');
const A='Any/Old/Alias', N='arbitrary.future.model', O='subscription-future';
const B={revision:'a'.repeat(64),model:'Any/Underlying-Model',connection:'connection-fixture'};
const P='10000000-0000-4000-8000-000000000001',C='10000000-0000-4000-8000-000000000002',S='10000000-0000-4000-8000-000000000003',T='10000000-0000-4000-8000-000000000004';
function temp(t){return makeTestDir(t,'neutral-guard-');}
function fixture(t,alias=A){
 const root=temp(t), counts={binding:0,parent:0,probe:0};
 const catalog={models:{[alias]:['minimal','high'],[O]:['high']},hubModels:{[alias]:B},managedOfficialModels:[O]};
 const input={hook_event_name:'PreToolUse',tool_name:'spawn_agent',tool_input:{model:alias,reasoning_effort:'high',message:'full synthetic task'}};
 const deps={options:{dataDir:root,policy},loadAvailableModels:()=>catalog,loadDisabledModels:()=>new Set(),
 guard:{...guard,senderContext:()=>({version:'v1'}),parentProvider:()=>null},
 resolveBindingKind:async model=>{assert.equal(model,alias);counts.binding++;return {...B,kind:'bound'};},
 resolveParentRoute:async(_,e)=>{counts.parent++;assert.equal(e.routeModel,alias);assert.equal(e.routeRevision,B.revision);return 'compatible';},
 getRouteStatus:()=>{counts.probe++;throw Error('probe forbidden');},
 currentSession:async()=>({}),senderContext:()=>({version:'v2'}),parentProvider:()=>null};
 return {root,counts,catalog,input,deps};
}
function denial(out,code){assert.equal(out?.hookSpecificOutput?.permissionDecision,'deny');assert.match(out.hookSpecificOutput.permissionDecisionReason,new RegExp(code));}
for(const alias of [A,N,'NoSlash','nested/arbitrary/model-z'])test('arbitrary exact binding and V1/V2: '+alias,async t=>{
 const f=fixture(t,alias);assert.equal(await runHook(f.input,f.deps),null);
 f.deps.guard.senderContext=()=>({version:'v2'});assert.equal(await runHook(f.input,f.deps),null);
 assert.deepEqual(f.counts,{binding:4,parent:2,probe:0});
});
test('disabled refuses before directory, binding or route reads',async t=>{
 const f=fixture(t);f.deps.loadDisabledModels=()=>new Set([A]);f.deps.loadAvailableModels=()=>{throw Error('must not load');};
 denial(await runHook(f.input,f.deps),'MODEL_DISABLED');assert.deepEqual(f.counts,{binding:0,parent:0,probe:0});
});
for(const state of ['disabled','unbound','unknown'])test('binding '+state+' rejects without route checks',async t=>{
 const f=fixture(t);f.deps.resolveBindingKind=async()=>({kind:state,revision:null});
 denial(await runHook(f.input,f.deps),'HUB_BINDING_UNAVAILABLE');assert.equal(f.counts.parent,0);
});
for(const field of ['revision','model','connection','kind'])test('binding switches '+field+' during proof',async t=>{
 const f=fixture(t);let reads=0;f.deps.resolveBindingKind=async()=>({...B,kind:'bound',...(++reads===2?{[field]:field==='revision'?'b'.repeat(64):'changed'}:{})});
 denial(await runHook(f.input,f.deps),'HUB_BINDING_CHANGED');
});
for(const route of ['unknown','official','incompatible'])test('unproven parent '+route,async t=>{
 const f=fixture(t);f.deps.resolveParentRoute=async()=>route;denial(await runHook(f.input,f.deps),'HUB_PARENT_ROUTE_UNKNOWN');
});
for(const message of ['',null,'界'.repeat(2667)])test('V2 incomplete/oversized task fails before parent '+String(message).length,async t=>{
 const f=fixture(t);f.deps.guard.senderContext=()=>({version:'v2'});f.input.tool_input.message=message;
 denial(await runHook(f.input,f.deps),'HUB_V2_TASK_INVALID');assert.equal(f.counts.parent,0);
});
test('unknown protocol and direct guard bypass refused',async t=>{
 const f=fixture(t);f.deps.guard.senderContext=()=>null;denial(await runHook(f.input,f.deps),'HUB_TASK_DELIVERY_UNSUPPORTED');
 denial(guard.handleHook(f.input,{...f.deps.options,availableModels:f.catalog}),'HUB_ROUTE_UNVERIFIED');
});
test('directory enumeration is local and does not depend on a historical line',async t=>{
 const f=fixture(t);f.deps.loadDisabledModels=()=>new Set([A]);
 const r=await listModels(f.deps);assert.deepEqual(r.models,{[O]:['high']});assert.equal(r.routeStatus,'pending_dispatch');
 assert.deepEqual(f.counts,{binding:0,parent:0,probe:0});
 delete f.catalog.models[A];delete f.catalog.hubModels[A];denial(await runHook(f.input,f.deps),'MODEL_DISABLED');
 f.deps.loadDisabledModels=()=>new Set();denial(await runHook(f.input,f.deps),'MODEL_NOT_ALLOWED');
});
test('managed subscription is directory data, not a model-name exemption',async t=>{
 const f=fixture(t);f.input.tool_input.model=O;assert.equal(await runHook(f.input,f.deps),null);assert.equal(f.counts.binding,0);
 f.catalog.managedOfficialModels=[];denial(await runHook(f.input,f.deps),'MODEL_NOT_ALLOWED');
});
test('recommendation uses same target proof and catches revision changes',async t=>{
 const f=fixture(t);assert.equal(await verifyManaged(A,B,f.deps),null);
 let n=0;f.deps.resolveBindingKind=async()=>({...B,kind:'bound',revision:++n===1?B.revision:'b'.repeat(64)});
 assert.equal(await verifyManaged(A,B,f.deps),'HUB_BINDING_CHANGED');
 f.deps.currentSession=async()=>null;n=0;assert.equal(await verifyManaged(A,B,f.deps),'HUB_DISPATCH_CHECK_PENDING');
});
test('saved manual preference selects exact alias without rewriting preference',async t=>{
 const f=fixture(t),r=await listModels(f.deps);
 const prefs={bounded:{model:A,effort:'high',fallback_model:O,fallback_effort:'high'}};
 const before=JSON.stringify(prefs);
 const selected=await recommendFromSnapshot('bounded',prefs,r,null,f.deps,{mode:'manual',preferencesSaved:true});
 assert.equal(selected.model,A);assert.equal(selected.dispatch_status,'route_checked');assert.equal(JSON.stringify(prefs),before);
});
test('V2 followups require current native subscription identity',async t=>{
 const f=fixture(t);f.deps.guard.senderContext=()=>({version:'v2'});
 const e={hook_event_name:'PreToolUse',tool_name:'send_message',tool_input:{target:'/root/task',message:'synthetic'}};
 for(const model of [A,null,'unregistered-model']){f.deps.messageTargetModel=async()=>model;denial(await runHook(e,f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');}
 f.deps.messageTargetModel=async()=>O;assert.equal(await runHook(e,f.deps),null);
 f.deps.loadDisabledModels=()=>new Set([O]);denial(await runHook(e,f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');
});
function recovery(t){
 const root=temp(t),parent=path.join(root,'parent.jsonl'),child=path.join(root,'child.jsonl'),now=Date.now();
 const args={model:A,task_name:'task',message:'完整 synthetic task'};
 const meta={type:'session_meta',payload:{id:C,session_id:S,parent_thread_id:P,model:A,multi_agent_version:'v2',model_provider:'custom',agent_path:'/root/task',timestamp:new Date(now-100).toISOString()}};
 const pm={type:'session_meta',payload:{id:P,agent_path:'/root'}},turn={type:'turn_context',payload:{turn_id:T}};
 const call={type:'response_item',timestamp:new Date(now-300).toISOString(),payload:{type:'function_call',namespace:'collaboration',name:'spawn_agent',call_id:'call-1',arguments:JSON.stringify(args)}};
 const launch={type:'event_msg',payload:{type:'item_completed',item:{type:'SubAgentActivity',kind:'started',agent_thread_id:C,model:A,id:'call-1'}}};
 const e={hook_event_name:'SubagentStart',model:A,agent_id:C,session_id:S,transcript_path:child};
 const options={dataDir:root,now};
 const write=(rows=[pm,turn,call,launch])=>{call.payload.arguments=JSON.stringify(args);fs.writeFileSync(parent,rows.map(JSON.stringify).join('\n'));fs.writeFileSync(child,JSON.stringify(meta));};
 write();return {root,parent,args,meta,pm,turn,call,launch,e,options,write,now};
}
test('V2 recovery preserves body; V1/plaintext skips recovery; exec wrappers fail closed',t=>{
 const f=recovery(t);assert.deepEqual(recoverTask(f.e,f.parent),{status:'restored',message:f.args.message});
 f.call.payload.encrypted_function_args=[];f.write();assert.equal(recoverTask(f.e,f.parent).status,'not_needed');
 f.launch.payload.item.id='exec-1';f.write();assert.equal(recoverTask(f.e,f.parent).status,'unavailable');
 f.meta.payload.multi_agent_version='v1';f.write();assert.equal(recoverTask(f.e,f.parent).status,'not_needed');
});
for(const change of ['parent','child','model','call','path','duplicate','duplicateLaunch','turn','missingLaunch','oversize','empty'])test('V2 identity refusal: '+change,t=>{
 const f=recovery(t);let rows;
 if(change==='parent')f.pm.payload.id=C;if(change==='child')f.meta.payload.id=P;
 if(change==='model')f.args.model=N;if(change==='call')f.launch.payload.item.id='different';
 if(change==='path')f.meta.payload.agent_path='/root/other';
 if(change==='duplicate')rows=[f.pm,f.turn,f.call,f.call,f.launch];
 if(change==='duplicateLaunch')rows=[f.pm,f.turn,f.call,f.launch,f.launch];
 if(change==='missingLaunch')rows=[f.pm,f.turn,f.call];
 if(change==='turn')rows=[f.pm,f.turn,f.call,{type:'turn_context',payload:{turn_id:P}},f.launch];
 if(change==='oversize')f.args.message='界'.repeat(2667);if(change==='empty')f.args.message=' ';
 f.write(rows);assert.equal(recoverTask(f.e,f.parent).status,'unavailable');
});
test('automatic V2 handoff requires same-turn selection record',t=>{
 const f=recovery(t);f.args.model='auto';f.write();assert.equal(recoverTask(f.e,f.parent,f.options).status,'unavailable');
 recordAutoSelection({session_id:P,turn_id:T,tool_use_id:'call-1',tool_input:f.args},{model:A,effort:'high'},{...f.options,now:f.now-200});
 assert.equal(recoverTask(f.e,f.parent,f.options).message,f.args.message);
 f.turn.payload.turn_id=P;f.write();assert.equal(recoverTask(f.e,f.parent,f.options).status,'unavailable');
});
test('startup restores bound model or explicitly tells child not to guess',async t=>{
 const f=fixture(t),r=recovery(t);f.deps.recoverStartupTask=e=>recoverTask(e,r.parent,r.options);
 assert.match((await runHook(r.e,f.deps)).hookSpecificOutput.additionalContext,/完整 synthetic task/);
 f.deps.loadDisabledModels=()=>new Set([A]);assert.match((await runHook(r.e,f.deps)).hookSpecificOutput.additionalContext,/不要猜测/);
});
test('Python metadata/engine/health proof is arbitrary-model and read-only, with network forbidden',t=>{
 const root=temp(t);
 const code=String.raw`
import sys,os,json,sqlite3,importlib.util,socket
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[sys.argv[1],str(Path(sys.argv[1])/'scripts')]
from storage import ProfileStore
import provider_hub_route as route
import importlib
parent=importlib.import_module('check-parent-route')
base=Path(sys.argv[2]);os.environ['LOCALAPPDATA']=str(base)
r=base/'CodexProviderHub';r.mkdir();dbpath=r/'profiles.sqlite'
spec=importlib.util.spec_from_file_location('fixture_binding',Path(sys.argv[1])/'scripts/binding-kind.py');b=importlib.util.module_from_spec(spec);spec.loader.exec_module(b)
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden'))
with sqlite3.connect(dbpath) as db:
 db.executescript('CREATE TABLE providers(id TEXT,name TEXT,protocol TEXT,base_url TEXT,model TEXT,encrypted_key BLOB);CREATE TABLE connection_models(provider_id TEXT,model TEXT,position INT,context_window INT,compact_limit INT,reasoning_levels TEXT,default_effort TEXT);CREATE TABLE engine_bindings(alias TEXT,provider_id TEXT,model TEXT,revision TEXT,account TEXT);CREATE TABLE model_routes(alias TEXT,provider_id TEXT,model TEXT);CREATE TABLE disabled_connections(provider_id TEXT);')
 p=('fixture','fixture','responses','https://example.invalid/v1','Opaque/Future',b'ciphertext-fixture')
 db.execute('INSERT INTO providers VALUES(?,?,?,?,?,?)',p)
 db.execute('INSERT INTO connection_models VALUES(?,?,?,?,?,?,?)',('fixture','Opaque/Future',0,None,None,None,None))
 rev=ProfileStore._content_revision(db,p)
 for alias in ['Legacy/Any/Alias','NoSlash']:
  db.execute('INSERT INTO engine_bindings VALUES(?,?,?,?,?)',(alias,'fixture','Opaque/Future',rev,'engine/account'))
  db.execute('INSERT INTO model_routes VALUES(?,?,?)',(alias,'fixture','Opaque/Future'))
 db.commit()
assert b.binding_kind(dbpath)['kind']=='unknown'
for alias in ['Legacy/Any/Alias','NoSlash']:
 result=b.binding_kind(dbpath,alias);assert result['kind']=='bound';assert result['model']=='Opaque/Future'
 assert 'example.invalid' not in json.dumps(result) and 'ciphertext' not in json.dumps(result)
engine=r/'engine-state';engine.mkdir()
config={'providers':{'engine':{'adapter':'openai-responses','baseUrl':'https://example.invalid/v1','apiKey':'keychain:engine/account','defaultModel':'Opaque/Future','models':['Opaque/Future']}}}
(engine/'config.json').write_text(json.dumps(config),encoding='utf-8')
identity=b.binding_kind(dbpath,'NoSlash');identity.pop('kind')
assert route.bound_engine_route('NoSlash',identity['revision'])==identity
assert route.bound_engine_route('NoSlash','b'*64) is None
local={'base_url':'http://127.0.0.1:24567/v1','http_headers':{'X-Provider-Hub-Token':'a'*40}}
with patch.object(route,'custom_route',return_value=local),patch.object(route,'health_request',side_effect=[(401,None),(200,{'service':'codex-provider-hub','version':1})]):
 assert route.active_source('NoSlash',identity['revision'])['binding']==identity
with sqlite3.connect(dbpath) as db:db.execute('INSERT INTO disabled_connections VALUES(?)',('fixture',));db.commit()
assert b.binding_kind(dbpath,'NoSlash')['kind']=='disabled'
with patch.object(route,'custom_route',return_value=local),patch.object(route,'health_request',side_effect=AssertionError('disabled must not probe')):
 assert route.active_source('NoSlash',identity['revision']) is None
with sqlite3.connect(dbpath) as db:
 db.execute('DELETE FROM disabled_connections');db.execute("UPDATE engine_bindings SET account='switched/account'");db.commit()
assert route.bound_engine_route('NoSlash',identity['revision']) is None
with sqlite3.connect(dbpath) as db:db.execute('DELETE FROM engine_bindings');db.commit()
assert b.binding_kind(dbpath,'NoSlash')['kind']=='unbound'
home=base/'codex';home.mkdir();os.environ['CODEX_HOME']=str(home)
(home/'config.toml').write_text('model_provider="custom"\n[model_providers.custom]\nbase_url="http://127.0.0.1:24567/v1"\n',encoding='utf-8')
source={'kind':'hub','base_url':'http://127.0.0.1:24567/v1','binding':identity}
session='10000000-0000-4000-8000-000000000001';turn='10000000-0000-4000-8000-000000000004'
with sqlite3.connect(home/'logs_2.sqlite') as db:
 db.execute('CREATE TABLE logs(id INTEGER PRIMARY KEY,thread_id TEXT,target TEXT,feedback_log_body TEXT)')
 db.execute('INSERT INTO logs(thread_id,target,feedback_log_body) VALUES(?,?,?)',(session,'codex_http_client::client',f'turn.id="{turn}": Request completed method=POST url=http://127.0.0.1:24567/v1/responses status=200'));db.commit()
with patch.object(parent,'active_source',return_value=source),patch.object(parent,'SAME_TURN_LOG_WAIT_SECONDS',0):
 assert parent.main(session,turn,'NoSlash',identity['revision'])=='compatible'
 assert parent.main(session,session,'NoSlash',identity['revision'])=='unknown'
with patch.object(parent,'active_source',side_effect=[source,None]),patch.object(parent,'SAME_TURN_LOG_WAIT_SECONDS',0):
 assert parent.main(session,turn,'NoSlash',identity['revision'])=='unknown'

print('synthetic metadata passed')
`;
 assert.match(execFileSync('python',['-B','-c',code,ROOT,root],{encoding:'utf8',env:{...process.env,PYTHONUTF8:'1',PYTHONDONTWRITEBYTECODE:'1'}}),/passed/);
});
test('guard and selection do not load supplier-specific status modules',()=>{
 const files=['hooks/guard.cjs','lib/guard.cjs','lib/v2-task-recovery.cjs','scripts/list-models.cjs','scripts/scheduling.cjs'];
 for(const file of files)assert.doesNotMatch(fs.readFileSync(path.join(ROOT,file),'utf8'),/require\([^)]*-status\.cjs['"]\)/i,file);
});

test('native identity survives list and recommendation when managed subscription list is empty',async t=>{
 const f=fixture(t);f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O];
 f.catalog.models['unmarked-native-lookalike']=['high'];
 const result=await listModels(f.deps);assert.deepEqual(result.nativeModels,[O]);
 assert.ok(result.models[O]);assert.equal(Object.hasOwn(result.models,'unmarked-native-lookalike'),false);
 const prefs={bounded:{model:O,effort:'high',fallback_model:A,fallback_effort:'high'}};
 const before=JSON.stringify(prefs);
 const chosen=await recommendFromSnapshot('bounded',prefs,result,null,f.deps,{mode:'manual',preferencesSaved:true});
 assert.equal(chosen.model,O);assert.equal(chosen.ok,true);assert.equal(JSON.stringify(prefs),before);
 assert.equal(f.counts.binding,0);assert.equal(f.counts.parent,0);
});

test('native spawn requires explicit identity plus current catalog membership',async t=>{
 const f=fixture(t);f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O];f.input.tool_input.model=O;
 assert.equal(await runHook(f.input,f.deps),null);
 f.catalog.nativeModels=[];denial(await runHook(f.input,f.deps),'MODEL_NOT_ALLOWED');
 f.catalog.nativeModels=[O];delete f.catalog.models[O];
 const out=await runHook(f.input,f.deps);assert.equal(out.hookSpecificOutput.permissionDecision,'deny');
 assert.deepEqual(f.counts,{binding:0,parent:0,probe:0});
});

test('native startup and V2 followup recognize trusted identity without guessing by name',async t=>{
 const f=fixture(t);f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O];
 f.deps.recoverStartupTask=()=>{throw Error('native must not use hub task recovery');};
 const start={hook_event_name:'SubagentStart',model:O};
 assert.doesNotMatch((await runHook(start,f.deps))?.hookSpecificOutput?.additionalContext || '',/不要猜测/);
 f.deps.guard.senderContext=()=>({version:'v2'});f.deps.messageTargetModel=async()=>O;
 const message={hook_event_name:'PreToolUse',tool_name:'send_message',tool_input:{target:'/root/native',message:'synthetic'}};
 assert.equal(await runHook(message,f.deps),null);
 f.catalog.nativeModels=[];
 assert.match((await runHook(start,f.deps)).hookSpecificOutput.additionalContext,/不要猜测/);
 denial(await runHook(message,f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');
 f.catalog.nativeModels=O; // Malformed string must not act like a trusted identity list.
 assert.match((await runHook(start,f.deps)).hookSpecificOutput.additionalContext,/不要猜测/);
 denial(await runHook(message,f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');
});

test('disabled or removed native identity cannot allow startup or V2 followup',async t=>{
 const f=fixture(t);f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O];
 f.deps.loadDisabledModels=()=>new Set([O]);f.input.tool_input.model=O;
 denial(await runHook(f.input,f.deps),'MODEL_DISABLED');
 const result=await listModels(f.deps);assert.equal(Object.hasOwn(result.models,O),false);
 assert.match((await runHook({hook_event_name:'SubagentStart',model:O},f.deps)).hookSpecificOutput.additionalContext,/不要猜测/);
 f.deps.guard.senderContext=()=>({version:'v2'});f.deps.messageTargetModel=async()=>O;
 const message={hook_event_name:'PreToolUse',tool_name:'send_message',tool_input:{target:'/root/native',message:'synthetic'}};
 denial(await runHook(message,f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');
 f.deps.loadDisabledModels=()=>new Set();delete f.catalog.models[O];
 denial(await runHook(message,f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');
 assert.deepEqual(f.counts,{binding:0,parent:0,probe:0});
});

test('native tag cannot bypass a conflicting Hub binding or its V2 safety',async t=>{
 const f=fixture(t);f.catalog.nativeModels=[A];f.catalog.managedOfficialModels=[];
 f.deps.guard.senderContext=()=>({version:'v2'});f.input.tool_input.message='';
 denial(await runHook(f.input,f.deps),'HUB_V2_TASK_INVALID');
 f.deps.messageTargetModel=async()=>A;
 denial(await runHook({hook_event_name:'PreToolUse',tool_name:'send_message',tool_input:{target:'/root/hub',message:'synthetic'}},f.deps),'HUB_V2_FOLLOWUP_UNSUPPORTED');
 f.catalog.hubModels[A]={};f.input.tool_input.message='full body';
 denial(await runHook(f.input,f.deps),'MODEL_NOT_ALLOWED');
 assert.match((await runHook({hook_event_name:'SubagentStart',model:A},f.deps)).hookSpecificOutput.additionalContext,/不要猜测/);
});

test('automatic routing preserves nativeModels through effort filtering and spawn',async t=>{
 const f=fixture(t);f.catalog.models={[O]:['high']};f.catalog.hubModels={};f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O];
 const scheduler=require('../lib/scheduling.cjs');
 t.mock.method(scheduler,'readRoutingSnapshot',()=>({preferences:{},mode:'auto',saved:false}));
 f.deps.readAutoSignals=async()=>({ok:true,evidence:{},references:{}});
 let recorded=false;f.deps.autoSelection={recordAutoSelection:(_,chosen)=>{assert.equal(chosen.model,O);recorded=true;}};
 f.input.tool_input.model='auto:bounded:text';
 const output=await runHook(f.input,f.deps);assert.equal(output.hookSpecificOutput.permissionDecision,'allow');
 assert.equal(output.hookSpecificOutput.updatedInput.model,O);assert.equal(recorded,true);
});

test('scheduling overview preserves native eligibility, unknown efforts and manual preference',t=>{
 const f=fixture(t);f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O,'native-unknown-effort'];
 f.catalog.models['native-unknown-effort']=[];f.catalog.models['unmarked-unknown-effort']=[];
 const scheduler=require('../lib/scheduling.cjs');
 const prefs={bounded:{model:O,effort:'high',fallback_model:A,fallback_effort:'high'}};
 t.mock.method(require('../lib/model-catalog.cjs'),'loadAvailableModels',()=>f.catalog);
 t.mock.method(require('../lib/disabled-models.cjs'),'loadDisabledModels',()=>new Set());
 t.mock.method(scheduler,'readRoutingSnapshot',()=>({preferences:prefs,mode:'manual',saved:true}));
 const file=require.resolve('../scripts/scheduling.cjs'),cached=require.cache[file];delete require.cache[file];
 t.after(()=>{delete require.cache[file];if(cached)require.cache[file]=cached;});
 const before=process.env.PLUGIN_DATA;process.env.PLUGIN_DATA=f.root;
 t.after(()=>{if(before===undefined)delete process.env.PLUGIN_DATA;else process.env.PLUGIN_DATA=before;});
 const module=require('../scripts/scheduling.cjs'),result=module.overview();
 assert.equal(result.effective.bounded.model,O);
 assert.equal(result.candidates.find(x=>x.model===O).critical_eligible,true);
 assert.equal(result.candidates.find(x=>x.model==='native-unknown-effort').state,'effort_unknown');
 assert.equal(result.candidates.find(x=>x.model==='unmarked-unknown-effort').state,'outside_policy');
 assert.deepEqual(result.preferences,prefs);
});

function policyEnvironment(t,f,prefs){
 const local=path.join(f.root,'local');
 const directory=path.join(local,'CodexProviderHub');
 fs.mkdirSync(directory,{recursive:true});
 const beforeLocal=process.env.LOCALAPPDATA,beforePlugin=process.env.PLUGIN_DATA;
 process.env.LOCALAPPDATA=local;process.env.PLUGIN_DATA=f.root;
 t.after(()=>{
  if(beforeLocal===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=beforeLocal;
  if(beforePlugin===undefined)delete process.env.PLUGIN_DATA;else process.env.PLUGIN_DATA=beforePlugin;
 });
 t.mock.method(require('../lib/model-catalog.cjs'),'loadAvailableModels',()=>f.catalog);
 t.mock.method(require('../lib/disabled-models.cjs'),'loadDisabledModels',()=>new Set());
 const scheduler=require('../lib/scheduling.cjs');
 t.mock.method(scheduler,'readRoutingSnapshot',()=>({preferences:prefs,mode:'manual',saved:true}));
 const modulePath=require.resolve('../scripts/scheduling.cjs'),cached=require.cache[modulePath];
 delete require.cache[modulePath];
 t.after(()=>{delete require.cache[modulePath];if(cached)require.cache[modulePath]=cached;});
 return {file:path.join(directory,'model-policy.json'),scheduling:require('../scripts/scheduling.cjs')};
}

test('default consumers all honor and reload the same optional local policy',async t=>{
 const f=fixture(t);delete f.deps.options.policy;
 const unrelated='unrelated-native-fixture';
 f.catalog.models[O]=['minimal','high'];f.catalog.models[unrelated]=['high'];
 f.catalog.managedOfficialModels=[];f.catalog.nativeModels=[O,unrelated];
 const prefs={bounded:{model:O,effort:'high',fallback_model:A,fallback_effort:'high'}};
 const env=policyEnvironment(t,f,prefs), originalPrefs=JSON.stringify(prefs);
 const restriction=effort=>({schemaVersion:2,allowedFamilies:[O],
   allowedEffortsByFamily:{[O]:[effort],[B.model]:['high']},jevAdvisory:{enabled:false}});
 const write=effort=>fs.writeFileSync(env.file,JSON.stringify(restriction(effort)),'utf8');
 const input=effort=>({model:O,reasoning_effort:effort});
 const guardOptions={availableModels:f.catalog,disabledModels:new Set()};

 // A missing optional file grants no extra identities and imposes no personal restriction.
 assert.deepEqual((await listModels(f.deps)).models[O],['minimal','high']);
 assert.equal(guard.validateSpawn(input('minimal'),guardOptions).ok,true);
 assert.deepEqual(env.scheduling.overview().candidates.find(row=>row.model===O).efforts,['minimal','high']);

 write('high');
 assert.equal(guard.validateSpawn(input('minimal'),guardOptions).code,'MODEL_EFFORT_NOT_ALLOWED');
 assert.equal(guard.validateSpawn(input('high'),guardOptions).ok,true);
 assert.equal(guard.validateSpawn({model:A,reasoning_effort:'minimal'},guardOptions).code,'MODEL_EFFORT_NOT_ALLOWED');
 assert.equal(guard.validateSpawn({model:unrelated,reasoning_effort:'high'},guardOptions).ok,false);
 const highList=await listModels(f.deps);
 assert.deepEqual(highList.models[O],['high']);
 assert.deepEqual(highList.models[A],['high']);
 assert.equal(Object.hasOwn(highList.models,unrelated),false);
 const highView=env.scheduling.overview();
 assert.deepEqual(highView.candidates.find(row=>row.model===O).efforts,['high']);
 assert.deepEqual(highView.candidates.find(row=>row.model===A).efforts,['high']);
 assert.equal(highView.candidates.find(row=>row.model===unrelated).state,'outside_policy');
 assert.equal(highView.effective.bounded.model,O);
 assert.equal(highView.effective.bounded.effort,'high');
 assert.equal(JSON.stringify(prefs),originalPrefs);

 // Editing the synthetic private file is observed without reloading the modules.
 write('minimal');
 assert.equal(guard.validateSpawn(input('high'),guardOptions).code,'MODEL_EFFORT_NOT_ALLOWED');
 assert.equal(guard.validateSpawn(input('minimal'),guardOptions).ok,true);
 assert.deepEqual((await listModels(f.deps)).models[O],['minimal']);
 assert.deepEqual(env.scheduling.overview().candidates.find(row=>row.model===O).efforts,['minimal']);
 assert.equal(JSON.stringify(prefs),originalPrefs);
 assert.deepEqual(JSON.parse(fs.readFileSync(env.file,'utf8')),restriction('minimal'));
});

for(const damaged of ['{invalid json','{"schemaVersion":2,"allowedFamilies":"invalid"}'])
test('damaged local policy fails closed without discarding recovered tasks',async t=>{
 const f=fixture(t);delete f.deps.options.policy;
 f.catalog.models[O]=['minimal','high'];f.input.tool_input.model=O;
 const prefs={bounded:{model:O,effort:'high',fallback_model:A,fallback_effort:'high'}};
 const env=policyEnvironment(t,f,prefs);
 fs.writeFileSync(env.file,damaged,'utf8');
 const guardOptions={availableModels:f.catalog,disabledModels:new Set()};
 assert.equal(guard.validateSpawn({model:O,reasoning_effort:'high'},guardOptions).ok,false);
 const response=await runHook(f.input,f.deps);
 assert.equal(response.hookSpecificOutput.permissionDecision,'deny');
 await assert.rejects(listModels(f.deps));
 assert.throws(()=>env.scheduling.overview());
 const recovered=guard.handleHook({hook_event_name:'SubagentStart'},
   {dataDir:f.root,taskRecovery:{status:'restored',message:'complete synthetic body'}});
 assert.match(recovered.hookSpecificOutput.additionalContext,/complete synthetic body/);
 assert.equal(fs.readFileSync(env.file,'utf8'),damaged);
});

test('explicit guard policy injection remains usable without widening default authorization',async t=>{
 const f=fixture(t),env=policyEnvironment(t,f,{});
 fs.writeFileSync(env.file,'{invalid json','utf8');
 const input={model:O,reasoning_effort:'high'};
 const common={availableModels:f.catalog,disabledModels:new Set()};
 assert.equal(guard.validateSpawn(input,common).ok,false);
 assert.equal(guard.validateSpawn(input,{...common,policy}).ok,true);
 const guidance=guard.handleHook({hook_event_name:'SubagentStart'},
   {dataDir:f.root,policy:{...policy,jevAdvisory:{enabled:true}}});
 assert.match(guidance.hookSpecificOutput.additionalContext,/按需辅助/);
 await assert.rejects(listModels(f.deps));
 assert.throws(()=>env.scheduling.overview());
});
