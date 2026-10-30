'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {TASKS,readPreferences,savePreferences,select}=require('../lib/scheduling.cjs');
const {listModels,recommendFromSnapshot}=require('../scripts/list-models.cjs');
const MODEL='fixture-route/model-one',UNBOUND='fixture-route/not-bound',NATIVE='native-fixture';
const binding={revision:'a'.repeat(64),model:'upstream-model-one',connection:'fixture-connection'};
const prefs=()=>Object.fromEntries(TASKS.map(task=>[task,{model:MODEL,effort:'high',fallback_model:NATIVE,fallback_effort:'medium'}]));
const catalog=()=>({models:{[MODEL]:['high'],[NATIVE]:['medium']},hubModels:{[MODEL]:binding},nativeModels:[NATIVE],managedOfficialModels:[]});
const proof=()=>({currentSession:async()=>({}),senderContext:()=>({version:'v1'}),parentProvider:()=>null,
 resolveBindingKind:async()=>({...binding,kind:'bound'}),resolveParentRoute:async()=> 'compatible'});
function local(t){const root=makeTestDir(t,'scheduling-dynamic-'),before=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
 t.after(()=>{if(before===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=before;});return root;}
test('a user-enabled binding is eligible without a model-name rule',()=>{
 const available={models:{[MODEL]:['medium','high']},hubModels:{[MODEL]:binding}};
 const result=select('bounded',prefs(),available,null,{mode:'auto',preferencesSaved:false});
 assert.equal(result.ok,true);assert.equal(result.model,MODEL);assert.equal(result.source,'automatic');
});
test('automatic selection uses catalog order and the saved user preference',()=>{
 const available=catalog(),p=prefs();
 assert.equal(select('bounded',p,available,null,{mode:'auto',preferencesSaved:false}).model,MODEL);
 p.bounded={model:NATIVE,effort:'medium',fallback_model:MODEL,fallback_effort:'high'};
 assert.equal(select('bounded',p,available,null,{mode:'auto',preferencesSaved:true}).model,NATIVE);
 p.bounded={model:MODEL,effort:'high',fallback_model:NATIVE,fallback_effort:'medium'};
 assert.equal(select('bounded',p,available,null,{mode:'auto',preferencesSaved:true}).model,MODEL);
});
test('enabled models can participate in critical work and unbound models remain ineligible',()=>{
 const available={models:{[MODEL]:['high'],[UNBOUND]:['high']},hubModels:{[MODEL]:binding}};
 assert.equal(select('critical',prefs(),available,null,{mode:'auto'}).model,MODEL);
 assert.equal(select('critical',prefs(),available,{model:MODEL,effort:'high'}).ok,true);
 assert.equal(select('bounded',prefs(),available,{model:UNBOUND,effort:'high'}).ok,false);
});
test('saved preferences survive a removed or disabled binding',t=>{
 local(t);const saved=prefs();savePreferences(saved);assert.deepEqual(readPreferences(),saved);
 const fallback={models:{[NATIVE]:['medium']},hubModels:{},nativeModels:[NATIVE]};
 assert.equal(select('bounded',readPreferences(),fallback,null,{mode:'manual'}).model,NATIVE);
 assert.equal(select('bounded',readPreferences(),fallback,{model:MODEL,effort:'high'}).code,'EXPLICIT_MODEL_UNAVAILABLE');
 assert.deepEqual(readPreferences(),saved);
});
test('recommendation checks exact parent route and stable binding for both protocol versions',async()=>{
 const snapshot=catalog(),p=prefs(),dependencies=proof();let reads=0;
 for(const version of ['v1','v2']){
  dependencies.senderContext=()=>({version});const result=await recommendFromSnapshot('bounded',p,snapshot,null,dependencies,{mode:'manual'});
  assert.equal(result.model,MODEL);assert.equal(result.dispatch_status,'route_checked');
 }
 dependencies.resolveBindingKind=async()=>({...binding,kind:'bound',revision:++reads%2===1?binding.revision:'b'.repeat(64)});
 let result=await recommendFromSnapshot('bounded',p,snapshot,null,dependencies,{mode:'manual'});
 assert.equal(result.model,NATIVE);assert.ok(result.skipped.some(item=>item.code==='HUB_BINDING_CHANGED'));
 reads=0;result=await recommendFromSnapshot('bounded',p,snapshot,{model:MODEL,effort:'high'},dependencies,{mode:'manual'});
 assert.equal(result.code,'EXPLICIT_MODEL_UNAVAILABLE');assert.equal(result.skipped[0].code,'HUB_BINDING_CHANGED');
});
test('disabled connection is excluded before binding or route checks',async()=>{
 const never=()=>{throw Error('disabled model must not be checked');};
 const result=await listModels({loadDisabledModels:()=>new Set([MODEL]),loadAvailableModels:catalog,
  currentSession:never,resolveBindingKind:never,resolveParentRoute:never});
 assert.equal(Object.hasOwn(result.models,MODEL),false);assert.ok(result.disabledModels.includes(MODEL));
});
test('several unreachable routes share a bounded recommendation deadline',async()=>{
 const second='fixture-route/model-two',secondBinding={revision:'b'.repeat(64),model:'upstream-model-two',connection:'connection-two'};
 const snapshot={models:{[MODEL]:['high'],[second]:['high'],[NATIVE]:['medium']},hubModels:{[MODEL]:binding,[second]:secondBinding},nativeModels:[NATIVE]};
 let clock=1000,checks=0;
 const dependencies={now:()=>clock,currentSession:async()=>({}),senderContext:()=>({version:'v2'}),parentProvider:()=>null,
  resolveBindingKind:async alias=>{checks++;return {...snapshot.hubModels[alias],kind:'bound'};},
  resolveParentRoute:async(_provider,input)=>{assert.ok(input.routeTimeoutMs<=1000);clock=4800;return 'unknown';}};
 const result=await recommendFromSnapshot('bounded',prefs(),snapshot,null,dependencies,{mode:'auto',preferencesSaved:false,deadline:5000});
 assert.equal(result.model,NATIVE);assert.ok(result.skipped.some(item=>item.code==='HUB_PRECHECK_TIMEOUT'));assert.equal(checks,2);
});
test('missing session leaves explicit request pending without checking a parent',async()=>{
 const deps=proof();deps.currentSession=async()=>null;deps.resolveParentRoute=()=>{throw Error('must not probe');};
 const result=await recommendFromSnapshot('bounded',prefs(),catalog(),{model:MODEL,effort:'high'},deps);
 assert.equal(result.model,MODEL);assert.equal(result.dispatch_status,'pending_dispatch');assert.equal(result.dispatch_code,'HUB_DISPATCH_CHECK_PENDING');
});
test('purpose samples are alias and effort specific and latest failure selects saved fallback',()=>{
 const p=prefs(),available=catalog();const evidence={[MODEL]:[{purpose:'tool',channel:'subagent_gateway',status:'passed',tested_at:'2026-10-30T00:00:00Z',effort:'high'}]};
 const options={mode:'manual',purpose:'tool',evidence};
 let result=select('bounded',p,available,null,options);assert.equal(result.model,MODEL);assert.equal(result.purpose_status,'sample_passed');
 result=select('bounded',p,available,{model:NATIVE,effort:'medium'},options);assert.equal(result.model,NATIVE);assert.equal(result.purpose_status,'pending_test');
 evidence[MODEL].push({purpose:'tool',channel:'subagent_gateway',status:'failed',tested_at:'2026-10-31T00:00:00Z',effort:'high'});
 assert.equal(select('bounded',p,available,null,options).model,NATIVE);
 evidence[MODEL]=[{purpose:'tool',channel:'other-channel',status:'passed',tested_at:'2026-10-31T00:00:00Z',effort:'high'}];
 assert.equal(select('bounded',p,available,null,options).purpose_status,'pending_test');
});
test('image evidence preserves explicit critical primary and backup preferences',()=>{
 const second='fixture-route/model-two',secondBinding={revision:'b'.repeat(64),model:'upstream-model-two',connection:'connection-two'};
 const p=prefs();p.critical={model:MODEL,effort:'high',fallback_model:second,fallback_effort:'high'};
 const available={models:{[second]:['high'],[MODEL]:['high']},hubModels:{[MODEL]:binding,[second]:secondBinding}};
 const sample={purpose:'image',channel:'subagent_gateway',status:'passed',tested_at:'2026-10-30T00:00:00Z',effort:'high'};
 const options={mode:'auto',preferencesSaved:true,purpose:'image',evidence:{[MODEL]:[sample],[second]:[sample]}};
 assert.equal(select('critical',p,available,null,options).model,MODEL);
 const removed={models:{[second]:['high']},hubModels:{[second]:secondBinding}};
 assert.equal(select('critical',p,removed,null,options).model,second);
});
test('manual critical preference never invents a third candidate after removal',()=>{
 const p=prefs(),available=catalog();assert.equal(select('critical',p,available,null,{mode:'manual'}).model,MODEL);
 const removed={models:{[NATIVE]:['medium']},nativeModels:[NATIVE]};
 assert.equal(select('critical',p,removed,null,{mode:'manual'}).model,NATIVE);
 assert.equal(select('critical',p,removed,{model:MODEL,effort:'high'}).code,'EXPLICIT_MODEL_UNAVAILABLE');
 assert.equal(select('critical',p,{models:{'other-native':['high']},nativeModels:['other-native']},null,{mode:'manual'}).ok,false);
});
test('temporary exclusions apply to one model and effort without changing preferences',()=>{
 const p=prefs(),saved=JSON.stringify(p),available=catalog();
 const result=select('bounded',p,available,null,{mode:'manual',purpose:'tool',excluded:[{model:MODEL,effort:'high'}]});
 assert.equal(result.model,NATIVE);assert.ok(result.skipped.some(row=>row.code==='SAMPLE_TEMPORARILY_EXCLUDED'));
 assert.equal(select('bounded',p,available,null,{mode:'manual',purpose:'tool'}).model,MODEL);assert.equal(JSON.stringify(p),saved);
});
