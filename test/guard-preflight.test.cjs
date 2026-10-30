'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {makeTestDir} = require('./safe-temp-fixture.cjs');
const {runHook} = require('../hooks/guard.cjs');
const guard = require('../lib/guard.cjs');
const MODEL='fixture-route/model-one', NATIVE='native-fixture';
const binding={revision:'a'.repeat(64),model:'fixture-upstream/model-one',connection:'connection-fixture'};
const policy={schemaVersion:2,allowedFamilies:[],allowedEffortsByFamily:{[binding.model]:['high','max']},jevAdvisory:{enabled:false}};
const catalog={models:{[MODEL]:['low','high','max'],[NATIVE]:['medium']},hubModels:{[MODEL]:binding},nativeModels:[NATIVE]};
const session='10000000-0000-4000-8000-000000000001';
const envelope=()=>({hook_event_name:'PreToolUse',tool_name:'spawn_agent',session_id:session,
  tool_call_id:'synthetic-call',tool_input:{model:MODEL,reasoning_effort:'high',message:'SECRET_TASK',api_key:'SECRET_KEY'}});
function fixture(t,overrides={}) {
  const root=makeTestDir(t,'guard-preflight-');
  const calls=[];
  const dependencies={options:{dataDir:root,policy},
    loadDisabledModels:()=>{calls.push('disabled');return new Set();},
    loadAvailableModels:()=>{calls.push('catalog');return catalog;},
    resolveBindingKind:async alias=>{assert.equal(alias,MODEL);calls.push('binding');return {...binding,kind:'bound'};},
    guard:{...guard,parentProvider:()=>{calls.push('provider');return 'custom';},senderContext:()=>{calls.push('context');return {version:'v1'};}},
    resolveParentRoute:async (_provider,input)=>{calls.push('parent');assert.equal(input.routeModel,MODEL);assert.equal(input.routeRevision,binding.revision);return 'compatible';},
    ...overrides};
  return {calls,dependencies,records:()=>fs.readFileSync(path.join(root,'audit.jsonl'),'utf8').trim().split('\n').map(JSON.parse)};
}
function finalAudit(f,code,status='denied') {
  const rows=f.records();assert.equal(rows.length,1);assert.equal(rows[0].event,'spawn.checked');
  assert.equal(rows[0].code,code);assert.equal(rows[0].status,status);
  assert.deepEqual(Object.keys(rows[0]).sort(),['schemaVersion','pluginVersion','timestamp','event','sessionId','callId','model','effort','status','code'].sort());
  assert.doesNotMatch(JSON.stringify(rows),/SECRET_TASK|SECRET_KEY|api_key|message|transcript/);
}
const cases=[
 ['disabled',e=>{e.tool_input.reasoning_effort='bad';},{loadDisabledModels:()=>new Set([MODEL]),loadAvailableModels:()=>{throw Error('catalog forbidden');}},'MODEL_DISABLED'],
 ['invalid effort',e=>{e.tool_input.reasoning_effort='bad';},{},'MODEL_EFFORT_UNSUPPORTED'],
 ['missing effort',e=>{delete e.tool_input.reasoning_effort;},{},'MODEL_EFFORT_REQUIRED'],
 ['user policy effort',e=>{e.tool_input.reasoning_effort='low';},{},'MODEL_EFFORT_NOT_ALLOWED'],
 ['unavailable',()=>{},{loadAvailableModels:()=>({models:{},nativeModels:[MODEL]})},'MODEL_UNAVAILABLE'],
 ['bad catalog',()=>{},{loadAvailableModels:()=>({models:[],nativeModels:[MODEL]})},'MODEL_CATALOG_UNAVAILABLE'],
 ['catalog read failure',()=>{},{loadAvailableModels:()=>{throw Object.assign(Error('catalog offline'),{code:'MODEL_CATALOG_UNAVAILABLE'});}},'MODEL_CATALOG_UNAVAILABLE'],
 ['policy invalid',()=>{},{options:{policy:{}}},'MODEL_POLICY_INVALID'],
 ['unidentified model',e=>{e.tool_input.model='unregistered-model';},{},'MODEL_NOT_ALLOWED'],
 ['missing model',e=>{delete e.tool_input.model;},{},'MODEL_REQUIRED']
];
for(const [name,change,overrides,code] of cases)test(`${name}: local rejection precedes binding and has one minimal audit`,async t=>{
 const f=fixture(t),e=envelope();change(e);Object.assign(f.dependencies,overrides,{options:{...f.dependencies.options,...overrides.options}});
 const output=await runHook(e,f.dependencies);
 assert.match(output.hookSpecificOutput.permissionDecisionReason,new RegExp('/'+code+']'));
 assert.ok(f.calls.every(c=>['disabled','catalog'].includes(c)));finalAudit(f,code);
});
test('enabled binding checks local selection, both binding snapshots and parent in order',async t=>{
 const f=fixture(t);assert.equal(await runHook(envelope(),f.dependencies),null);
 assert.deepEqual(f.calls,['disabled','catalog','binding','context','provider','parent','binding']);
 finalAudit(f,'MODEL_SELECTION_VALID','allowed');
});
test('explicit native identity can be selected without a binding or parent probe',async t=>{
 const f=fixture(t),e=envelope();e.tool_input.model=NATIVE;e.tool_input.reasoning_effort='medium';
 assert.equal(await runHook(e,f.dependencies),null);assert.deepEqual(f.calls,['disabled','catalog']);finalAudit(f,'MODEL_SELECTION_VALID','allowed');
});
for(const route of ['official','unknown','incompatible'])test(`unproven parent ${route} refuses with one audit`,async t=>{
 const f=fixture(t);f.dependencies.resolveParentRoute=async()=>route;
 assert.match((await runHook(envelope(),f.dependencies)).hookSpecificOutput.permissionDecisionReason,/HUB_PARENT_ROUTE_UNKNOWN/);
 assert.equal(f.calls.filter(c=>c==='binding').length,2);finalAudit(f,'HUB_PARENT_ROUTE_UNKNOWN');
});
for(const kind of ['unknown','unbound','disabled'])test(`${kind} binding rejects before parent`,async t=>{
 const f=fixture(t);f.dependencies.resolveBindingKind=async()=>({kind,revision:null});
 await runHook(envelope(),f.dependencies);assert.ok(!f.calls.includes('provider'));finalAudit(f,'HUB_BINDING_UNAVAILABLE');
});
for(const field of ['revision','model','connection','kind'])test(`binding race on ${field} has one denial`,async t=>{
 let reads=0;const f=fixture(t);
 f.dependencies.resolveBindingKind=async()=>({...binding,kind:'bound',...(++reads===2?{[field]:field==='revision'?'b'.repeat(64):'changed'}:{})});
 await runHook(envelope(),f.dependencies);assert.equal(reads,2);finalAudit(f,'HUB_BINDING_CHANGED');
});
test('V2 oversized task rejects before parent proof',async t=>{
 const f=fixture(t),e=envelope();e.tool_input.message='x'.repeat(8001);f.dependencies.guard.senderContext=()=>({version:'v2'});
 await runHook(e,f.dependencies);assert.ok(!f.calls.includes('parent'));finalAudit(f,'HUB_V2_TASK_INVALID');
});
test('direct guard cannot bypass binding and parent preflight',t=>{
 const f=fixture(t),out=guard.handleHook(envelope(),{...f.dependencies.options,availableModels:catalog});
 assert.match(out.hookSpecificOutput.permissionDecisionReason,/HUB_ROUTE_UNVERIFIED/);
});
test('untrusted tool payload cannot inject a successful preflight',t=>{
 const f=fixture(t),e=envelope();e.tool_input.reasoning_effort='bad';e.tool_input.spawnValidation={ok:true};
 assert.match(guard.handleHook(e,{...f.dependencies.options,availableModels:catalog}).hookSpecificOutput.permissionDecisionReason,/MODEL_EFFORT_UNSUPPORTED/);
 finalAudit(f,'MODEL_EFFORT_UNSUPPORTED');
});
test('startup recovery and followup refusal keep task text out of audit',t=>{
 const f=fixture(t);
 const output=guard.handleHook({hook_event_name:'SubagentStart',model:MODEL},{...f.dependencies.options,taskRecovery:{status:'restored',message:'SECRET_TASK'}});
 assert.match(output.hookSpecificOutput.additionalContext,/SECRET_TASK/);assert.doesNotMatch(JSON.stringify(f.records()),/SECRET_TASK/);
 const missing=guard.handleHook({hook_event_name:'SubagentStart',model:MODEL},{...f.dependencies.options,taskRecovery:{status:'unavailable'}});
 assert.match(missing.hookSpecificOutput.additionalContext,/任务正文未送达/);
 const followup=guard.handleHook({hook_event_name:'PreToolUse',tool_name:'send_message'},
   {...f.dependencies.options,agentVersion:'v2',targetModel:MODEL,targetHub:true});
 assert.match(followup.hookSpecificOutput.permissionDecisionReason,/HUB_V2_FOLLOWUP_UNSUPPORTED/);
 assert.doesNotMatch(JSON.stringify(f.records()),/SECRET_TASK|SECRET_KEY/);
});
test('parent proof errors still recheck binding and produce one final denial',async t=>{
 const f=fixture(t);f.dependencies.resolveParentRoute=async()=>{throw Error('proof failed');};
 await runHook(envelope(),f.dependencies);assert.equal(f.calls.filter(c=>c==='binding').length,2);finalAudit(f,'HUB_PARENT_ROUTE_UNKNOWN');
});
test('binding read errors reject safely before parent proof',async t=>{
 const f=fixture(t);f.dependencies.resolveBindingKind=async()=>{throw Error('fixture unavailable');};
 await runHook(envelope(),f.dependencies);assert.deepEqual(f.calls,['disabled','catalog']);finalAudit(f,'HUB_BINDING_UNAVAILABLE');
});
test('post-spawn, wait and stop continue matching proven child settings',t=>{
 const f=fixture(t),e=envelope(),id='10000000-0000-4000-8000-000000000003';
 const root=f.dependencies.options.dataDir,transcript=path.join(root,'parent.jsonl');
 fs.writeFileSync(transcript,JSON.stringify({type:'session_meta',payload:{id:e.session_id}})+'\n');
 fs.writeFileSync(path.join(root,'rollout-'+id+'.jsonl'),[
  {type:'session_meta',payload:{id,parent_thread_id:e.session_id}},{type:'turn_context',payload:{model:MODEL,effort:'high'}}].map(JSON.stringify).join('\n'));
 const options={...f.dependencies.options,sessionsRoot:root};
 assert.equal(guard.handleHook({...e,transcript_path:transcript,hook_event_name:'PostToolUse',tool_response:{agent_id:id}},options),null);
 assert.equal(guard.handleHook({...e,transcript_path:transcript,hook_event_name:'PostToolUse',tool_name:'wait_agent',tool_input:{targets:[id]}},options),null);
 assert.equal(guard.handleHook({...e,transcript_path:transcript,hook_event_name:'SubagentStop',agent_id:id},options),null);
 assert.equal(f.records().length,3);assert.ok(f.records().every(r=>r.event==='runtime.checked' && r.status==='matched'));
});
test('binding without a revision cannot pass the proof',async t=>{
 const f=fixture(t);f.dependencies.resolveBindingKind=async()=>({...binding,kind:'bound',revision:null});
 await runHook(envelope(),f.dependencies);assert.ok(!f.calls.includes('parent'));finalAudit(f,'HUB_BINDING_UNAVAILABLE');
});
test('audit failure preserves local rejection and fails closed for an otherwise allowed model',async t=>{
 const f=fixture(t),e=envelope(),file=path.join(f.dependencies.options.dataDir,'not-a-directory');fs.writeFileSync(file,'fixture');
 f.dependencies.options.dataDir=file;f.dependencies.loadDisabledModels=()=>new Set([MODEL]);
 assert.match((await runHook(e,f.dependencies)).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED/);
 e.tool_input.model=NATIVE;e.tool_input.reasoning_effort='medium';f.dependencies.loadDisabledModels=()=>new Set();
 assert.match((await runHook(e,f.dependencies)).hookSpecificOutput.permissionDecisionReason,/MODEL_GUARD_UNAVAILABLE/);
});
