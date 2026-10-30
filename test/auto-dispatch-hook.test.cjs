'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {runHook}=require('../hooks/guard.cjs');
const realGuard=require('../lib/guard.cjs');

const A='hub-11111111111141118111111111111111/model-a';
const B='hub-22222222222242228222222222222222/model-b';
const NATIVE='native-fixture';
const binding=model=>({revision:'a'.repeat(64),model,connection:model});
const catalog={models:{[A]:['high'],[B]:['high'],[NATIVE]:['high']},hubModels:{[A]:binding('model-a'),[B]:binding('model-b')},nativeModels:[NATIVE]};
const sample={purpose:'tool',channel:'subagent_gateway',effort:'high',status:'passed',tested_at:'2026-10-30T00:00:00Z'};
const ref=score=>({status:'available',results:[{benchmark:'terminal-bench 2.1',task_id:'tb21',unit:'%',
  conditions_fingerprint:'a'.repeat(64),score,verified:false}]});
const signal={ok:true,evidence:{[A]:[sample],[B]:[sample]},references:{[A]:ref(40),[B]:ref(90)}};

function fixture(t,patch={}) {
  const root=makeTestDir(t,'hub-auto-hook-');
  const old=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
  t.after(()=>{if(old===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=old;});
  const saved=[];
  const guard={...realGuard,senderContext:()=>({version:'v1'}),parentProvider:()=> 'custom'};
  const transcript=path.join(root,'synthetic-parent.jsonl');
  fs.writeFileSync(transcript,JSON.stringify({type:'session_meta',payload:{id:'44444444-4444-4444-8444-444444444444'}})+'\n');
  const options={dataDir:path.join(root,'audit'),availableModels:catalog};
  const dependencies={guard,options,loadDisabledModels:()=>new Set(),loadAvailableModels:()=>catalog,
    readAutoSignals:async()=>signal, resolveBindingKind:async alias=>({...catalog.hubModels[alias],kind:'bound'}),
    resolveParentRoute:async()=> 'compatible',
    autoSelection:{recordAutoSelection:(input,selected)=>{saved.push({input,selected});}} ,...patch};
  const input={hook_event_name:'PreToolUse',tool_name:'spawn_agent',
    session_id:'44444444-4444-4444-8444-444444444444',turn_id:'55555555-5555-4555-8555-555555555555',
    tool_use_id:'call_fixture',transcript_path:transcript,
    tool_input:{model:'auto:bounded:tool',message:'synthetic task',task_name:'work',fork_turns:'none'}};
  return {input,dependencies,saved};
}

test('auto dispatch selects using task evidence and validates the final model before rewriting',async t=>{
  const {input,dependencies,saved}=fixture(t);
  const result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'allow');
  assert.deepEqual(result.hookSpecificOutput.updatedInput,{model:B,reasoning_effort:'high',message:'synthetic task',task_name:'work',fork_turns:'none'});
  assert.equal(saved.length,1);assert.equal(saved[0].selected.model,B);
  assert.equal(input.tool_input.model,'auto:bounded:tool');
});

test('explicit model stays unchanged and never calls automatic selection',async t=>{
  const {input,dependencies,saved}=fixture(t,{readAutoSignals:async()=>{throw Error('must not read signals');}});
  input.tool_input={model:NATIVE,reasoning_effort:'high',message:'synthetic task'};
  assert.equal(await runHook(input,dependencies),null);assert.equal(saved.length,0);
});

test('a rejected automatic route falls through to another validated candidate without dispatching the first',async t=>{
  const {input,dependencies,saved}=fixture(t);
  dependencies.resolveBindingKind=async alias=>alias===B ? {kind:'disabled'} : {...catalog.hubModels[alias],kind:'bound'};
  const result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'allow');
  assert.notEqual(result.hookSpecificOutput.updatedInput.model,B);
  assert.equal(saved.length,1);
});

test('automatic selection and recording failures refuse dispatch',async t=>{
  let {input,dependencies}=fixture(t,{readAutoSignals:async()=>({ok:false})});
  let result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'deny');
  ({input,dependencies}=fixture(t,{autoSelection:{recordAutoSelection:()=>{throw Error('storage error');}}}));
  result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'deny');
});

test('a specified effort constrains automatic selection and unsupported effort refuses dispatch',async t=>{
  const {input,dependencies,saved}=fixture(t);
  input.tool_input.reasoning_effort='max';
  const result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(saved.length,0);
});

test('auto respects an explicit supported effort and preserves all other spawn arguments',async t=>{
  const {input,dependencies}=fixture(t);
  input.tool_input.reasoning_effort='high';
  input.tool_input.items=[{type:'text',text:'synthetic fixture'}];
  const result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.updatedInput.reasoning_effort,'high');
  assert.deepEqual(result.hookSpecificOutput.updatedInput.items,input.tool_input.items);
});

test('explicit low effort can be used for an eligible independently configured model',async t=>{
  const {input,dependencies}=fixture(t);
  const dynamic={models:{...catalog.models,[NATIVE]:['low','high']},hubModels:catalog.hubModels,nativeModels:catalog.nativeModels};
  dependencies.options.availableModels=dynamic;
  dependencies.loadAvailableModels=()=>dynamic;
  input.tool_input.model='auto:critical:text';input.tool_input.reasoning_effort='low';
  const result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'allow');
  assert.equal(result.hookSpecificOutput.updatedInput.model,NATIVE);
  assert.equal(result.hookSpecificOutput.updatedInput.reasoning_effort,'low');
});

test('automatic V2 dispatch applies the existing task delivery guard to the final route',async t=>{
  const {input,dependencies}=fixture(t);
  dependencies.guard.senderContext=()=>({version:'v2'});
  input.tool_input.message='x'.repeat(8001);
  const result=await runHook(input,dependencies);
  assert.equal(result.hookSpecificOutput.permissionDecision,'allow');
  assert.equal(result.hookSpecificOutput.updatedInput.model,NATIVE);
  assert.equal(result.hookSpecificOutput.updatedInput.message,input.tool_input.message);
});

test('paused candidates are excluded before their route is checked',async t=>{
  const {input,dependencies}=fixture(t);
  dependencies.loadDisabledModels=()=>new Set([B]);
  dependencies.resolveBindingKind=async alias=>{
    assert.notEqual(alias,B);return {...catalog.hubModels[alias],kind:'bound'};
  };
  const result=await runHook(input,dependencies);
  assert.notEqual(result.hookSpecificOutput.updatedInput.model,B);
});

test('automatic dispatch uses the saved task preference and strength',async t=>{
  const {input,dependencies}=fixture(t);
  const settings=require('../lib/scheduling.cjs');
  const prefs=Object.fromEntries(settings.TASKS.map(task=>[task,{model:A,effort:'high',fallback_model:B,fallback_effort:'high'}]));
  prefs.boundary={model:A,effort:'max',fallback_model:NATIVE,fallback_effort:'high'};
  settings.saveRoutingPreferences(prefs);
  const dynamic={models:{...catalog.models,[A]:['high','max']},hubModels:catalog.hubModels,nativeModels:catalog.nativeModels};
  dependencies.options.availableModels=dynamic;
  dependencies.loadAvailableModels=()=>dynamic;
  dependencies.readAutoSignals=async()=>({ok:true,evidence:{},references:{}});
  input.tool_input.model='auto:boundary:tool';
  const out=await runHook(input,dependencies);
  assert.equal(out.hookSpecificOutput.permissionDecision,'allow');
  assert.equal(out.hookSpecificOutput.updatedInput.model,A);
  assert.equal(out.hookSpecificOutput.updatedInput.reasoning_effort,'max');
});

test('manual dispatch refuses an unconfigured third model when both chosen routes fail',async t=>{
  const {input,dependencies,saved}=fixture(t);
  const settings=require('../lib/scheduling.cjs');
  const prefs=Object.fromEntries(settings.TASKS.map(task=>[task,{model:A,effort:'high',fallback_model:B,fallback_effort:'high'}]));
  prefs.bounded={model:A,effort:'high',fallback_model:B,fallback_effort:'high'};
  settings.saveManualRouting(prefs);
  dependencies.resolveBindingKind=async()=>({kind:'disabled'});
  const out=await runHook(input,dependencies);
  assert.equal(out.hookSpecificOutput.permissionDecision,'deny');
  assert.equal(saved.length,0);
});
