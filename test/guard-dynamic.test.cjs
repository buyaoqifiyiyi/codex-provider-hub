'use strict';
const test=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {runHook}=require('../hooks/guard.cjs');
const guard=require('../lib/guard.cjs'), policy=require('../policy.json');
const {validateSpawn,eligibleModels}=require('../lib/model-policy.cjs');
const alias='fixture/team/model-x', binding={revision:'a'.repeat(64),model:'fixture/model-x',connection:'synthetic'};
const catalog=()=>({models:{[alias]:['minimal','high'],'native-future':['minimal'],'other-unbound':['high']},hubModels:{[alias]:binding},managedOfficialModels:['native-future']});
const input=()=>({hook_event_name:'PreToolUse',tool_name:'spawn_agent',tool_input:{model:alias,reasoning_effort:'minimal',message:'synthetic task'}});
function fixture(t){
 const root=makeTestDir(t,'guard-dynamic-');
 let reads=0,parent=0;
 const deps={options:{dataDir:root,policy},loadDisabledModels:()=>new Set(),loadAvailableModels:catalog,
 guard:{...guard,senderContext:()=>({version:'v1'}),parentProvider:()=>null},
 resolveBindingKind:async model=>{assert.equal(model,alias);reads++;return {...binding,kind:'bound'};},
 resolveParentRoute:async(provider,e)=>{parent++;assert.equal(e.routeModel,alias);assert.equal(e.routeRevision,binding.revision);return 'compatible';}};
 return {deps,counts:()=>[reads,parent]};
}
test('dynamic exact IDs and managed official candidate; catalog alone is not authorization',()=>{
 const c=catalog(); assert.deepEqual(Object.keys(eligibleModels(policy,c)),[alias,'native-future']);
 assert.equal(validateSpawn(input().tool_input,policy,c).ok,true);
 assert.equal(validateSpawn({...input().tool_input,model:'other-unbound',reasoning_effort:'high'},policy,c).code,'MODEL_NOT_ALLOWED');
 delete c.hubModels; assert.equal(validateSpawn(input().tool_input,policy,c).code,'MODEL_NOT_ALLOWED');
 assert.equal(validateSpawn({...input().tool_input,model:'native-future'},policy,c).ok,true);
 c.managedOfficialModels=[];assert.equal(validateSpawn({...input().tool_input,model:'native-future'},policy,c).code,'MODEL_NOT_ALLOWED');
});
test('catalog support and user policy jointly determine efforts for a bound model',()=>{
 const c=catalog();c.models[alias]=['high'];assert.equal(validateSpawn(input().tool_input,policy,c).code,'MODEL_EFFORT_UNSUPPORTED');
 c.models[alias]=['minimal','low','high','max'];
 const restricted={...policy,allowedEffortsByFamily:{[binding.model]:['high','max']}};
 assert.deepEqual(eligibleModels(restricted,c)[alias],['high','max']);
 assert.equal(validateSpawn(input().tool_input,restricted,c).code,'MODEL_EFFORT_NOT_ALLOWED');
 assert.equal(validateSpawn({...input().tool_input,reasoning_effort:'high'},restricted,c).ok,true);
});
test('V1 dynamic binding checks target parent proof and reads binding twice',async t=>{
 const f=fixture(t);assert.equal(await runHook(input(),f.deps),null);assert.deepEqual(f.counts(),[2,1]);
});
for(const route of ['unknown','official','incompatible'])test('dynamic parent '+route+' fails closed',async t=>{
 const f=fixture(t);f.deps.resolveParentRoute=async()=>route;
 assert.match((await runHook(input(),f.deps)).hookSpecificOutput.permissionDecisionReason,/HUB_PARENT_ROUTE_UNKNOWN/);
});
test('dynamic binding revision race fails closed',async t=>{
 const f=fixture(t);let n=0;f.deps.resolveBindingKind=async()=>({...binding,kind:'bound',revision:++n===1?binding.revision:'b'.repeat(64)});
 assert.match((await runHook(input(),f.deps)).hookSpecificOutput.permissionDecisionReason,/HUB_BINDING_CHANGED/);
});
for(const version of [null])test('dynamic unverified delivery '+version+' rejected before parent',async t=>{
 const f=fixture(t);f.deps.guard.senderContext=()=>({version});
 assert.match((await runHook(input(),f.deps)).hookSpecificOutput.permissionDecisionReason,/HUB_TASK_DELIVERY_UNSUPPORTED/);assert.deepEqual(f.counts(),[1,0]);
});
test('disabled dynamic model performs no catalog, binding or route reads',async t=>{
 const f=fixture(t);f.deps.loadDisabledModels=()=>new Set([alias]);f.deps.loadAvailableModels=()=>{throw Error('forbidden');};
 assert.match((await runHook(input(),f.deps)).hookSpecificOutput.permissionDecisionReason,/MODEL_DISABLED/);assert.deepEqual(f.counts(),[0,0]);
});
test('subscription candidate skips Hub route',async t=>{
 const f=fixture(t),e=input();e.tool_input.model='native-future';assert.equal(await runHook(e,f.deps),null);assert.deepEqual(f.counts(),[0,0]);
});
test('direct guard cannot bypass dynamic route preflight',t=>{
 const f=fixture(t);assert.match(guard.handleHook(input(),{...f.deps.options,availableModels:catalog()}).hookSpecificOutput.permissionDecisionReason,/HUB_ROUTE_UNVERIFIED/);
});
test('V2 new hub and unresolved targets reject followup',t=>{
 const f=fixture(t),e={hook_event_name:'PreToolUse',tool_name:'send_message'};
 for(const targetModel of [alias,null]) assert.match(guard.handleHook(e,{...f.deps.options,agentVersion:'v2',targetModel,targetHub:!!targetModel}).hookSpecificOutput.permissionDecisionReason,/HUB_V2_FOLLOWUP_UNSUPPORTED/);
});

test('V2 dynamic initial task permits bounded recovery path',async t=>{
 const f=fixture(t);f.deps.guard.senderContext=()=>({version:'v2'});
 assert.equal(await runHook(input(),f.deps),null);assert.deepEqual(f.counts(),[2,1]);
 const e=input();e.tool_input.message='x'.repeat(8001);
 assert.match((await runHook(e,f.deps)).hookSpecificOutput.permissionDecisionReason,/HUB_V2_TASK_INVALID/);
});
const {recoverTask,recoverStartupTask}=require('../lib/v2-task-recovery.cjs');
const LEGACY='legacy-fixture/model-x';
function recoveryFixture(t,model=alias){
 const root=makeTestDir(t,'guard-recovery-');
 const parentId='10000000-0000-4000-8000-000000000001',agentId='10000000-0000-4000-8000-000000000002',sessionId='10000000-0000-4000-8000-000000000003';
 const child=path.join(root,'child.jsonl'),parent=path.join(root,'parent.jsonl');
 const e={hook_event_name:'SubagentStart',model,agent_id:agentId,session_id:sessionId,transcript_path:child};
 const meta={type:'session_meta',payload:{id:agentId,session_id:sessionId,parent_thread_id:parentId,multi_agent_version:'v2',model_provider:'custom',agent_path:'/root/task',timestamp:'2026-10-29T00:00:01Z'}};
 const call={type:'response_item',timestamp:'2026-10-29T00:00:00Z',payload:{type:'function_call',namespace:'collaboration',name:'spawn_agent',call_id:'call-1',arguments:JSON.stringify({model,task_name:'task',message:'synthetic full task'})}};
 const launch={type:'event_msg',payload:{type:'item_completed',item:{type:'SubAgentActivity',kind:'started',agent_thread_id:agentId,model,id:'call-1'}}};
 const parentMeta={type:'session_meta',payload:{id:parentId,agent_path:'/root'}};
 const turn={type:'turn_context',payload:{turn_id:'10000000-0000-4000-8000-000000000004'}};
 const write=(records=[parentMeta,call,launch],children=[meta])=>{records=[records[0],turn,...records.slice(1)];fs.writeFileSync(parent,records.map(JSON.stringify).join('\n'));fs.writeFileSync(child,children.map(JSON.stringify).join('\n'));};
 write();return {e,meta,call,launch,parentMeta,parent,write};
}
for(const model of [alias,LEGACY])test('V2 recovery exact initial model '+model,async t=>{
 const f=recoveryFixture(t,model);
 assert.deepEqual(recoverTask(f.e,f.parent),{status:'restored',message:'synthetic full task'});
 assert.deepEqual(await recoverStartupTask(f.e,async()=>f.parent),{status:'restored',message:'synthetic full task'});
 f.call.payload.encrypted_function_args=[];f.write();assert.equal(recoverTask(f.e,f.parent).status,'not_needed');
});
for(const change of ['model','parent','path','callId','duplicateCall','duplicateLaunch','oversize','childModel'])test('V2 ambiguous or mismatched '+change+' stops',t=>{
 const f=recoveryFixture(t);let rows;
 if(change==='model')f.call.payload.arguments=JSON.stringify({model:'other',task_name:'task',message:'wrong'});
 if(change==='parent')f.parentMeta.payload.id='10000000-0000-4000-8000-000000000006';
 if(change==='path')f.meta.payload.agent_path='/root/other';
 if(change==='callId')f.launch.payload.item.id='different';
 if(change==='duplicateCall')rows=[f.parentMeta,f.call,f.call,f.launch];
 if(change==='duplicateLaunch')rows=[f.parentMeta,f.call,f.launch,f.launch];
 if(change==='oversize')f.call.payload.arguments=JSON.stringify({model:alias,task_name:'task',message:'x'.repeat(8001)});
 if(change==='childModel')f.meta.payload.model='wrong';
 f.write(rows);assert.equal(recoverTask(f.e,f.parent).status,'unavailable');
});
test('V2 waits for the child launch identity before recovering any task',t=>{
 const f=recoveryFixture(t);f.write([f.parentMeta,f.call]);assert.equal(recoverTask(f.e,f.parent).status,'unavailable');
 f.write([f.parentMeta,f.call,f.call]);assert.equal(recoverTask(f.e,f.parent).status,'unavailable');
});
test('V2 generic startup hook restores only current managed model and stops missing binding',async t=>{
 const f=fixture(t),r=recoveryFixture(t);
 f.deps.recoverStartupTask=input=>recoverStartupTask(input,async()=>r.parent);
 const output=await runHook(r.e,f.deps);assert.match(output.hookSpecificOutput.additionalContext,/synthetic full task/);
 f.deps.loadAvailableModels=()=>({models:{[alias]:['minimal']},hubModels:{}});
 assert.match((await runHook(r.e,f.deps)).hookSpecificOutput.additionalContext,/任务正文未送达/);
});
test('startup still delivers an exact recovered task when the audit location is unavailable',t=>{
 const root=makeTestDir(t,'guard-audit-failure-');
 const blocked=path.join(root,'not-a-directory');fs.writeFileSync(blocked,'fixture');
 const out=guard.handleHook({hook_event_name:'SubagentStart',agent_id:'10000000-0000-4000-8000-000000000002'},
   {dataDir:blocked,taskRecovery:{status:'restored',message:'exact synthetic task'}});
 assert.match(out.hookSpecificOutput.additionalContext,/exact synthetic task/);
 assert.match(out.systemMessage,/审计/);
});
test('startup still warns the child not to guess when audit and task delivery are unavailable',t=>{
 const root=makeTestDir(t,'guard-audit-failure-');
 const blocked=path.join(root,'not-a-directory');fs.writeFileSync(blocked,'fixture');
 const out=guard.handleHook({hook_event_name:'SubagentStart'},{dataDir:blocked,taskRecovery:{status:'unavailable'}});
 assert.match(out.hookSpecificOutput.additionalContext,/不要猜测/);
});
test('V2 generic followup hook rejects managed and unresolved targets',async t=>{
 const f=fixture(t);f.deps.guard.senderContext=()=>({version:'v2'});
 for(const model of [alias,null]){
  f.deps.messageTargetModel=async()=>model;
  const out=await runHook({hook_event_name:'PreToolUse',tool_name:'send_message',tool_input:{target:'/root/task',message:'synthetic'}},f.deps);
  assert.match(out.hookSpecificOutput.permissionDecisionReason,/HUB_V2_FOLLOWUP_UNSUPPORTED/);
 }
});
test('a newly bound underlying model uses the same proof without a supplier probe',async t=>{
 const f=fixture(t),b={...binding,model:'group/nested/future-model'};
 f.deps.loadAvailableModels=()=>({models:{[alias]:['high']},hubModels:{[alias]:b}});
 f.deps.resolveBindingKind=async()=>({...b,kind:'bound'});
 const e=input();e.tool_input.reasoning_effort='high';
 assert.equal(await runHook(e,f.deps),null);
 assert.deepEqual(f.counts(),[0,1]);
});
test('unbound alias cannot fall through family authorization',()=>{
 const model='hub-synthetic/native-looking';
 assert.equal(validateSpawn({model,reasoning_effort:'high'},policy,{models:{[model]:['high']}}).code,'MODEL_NOT_ALLOWED');
});
