'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const scheduling=require('../lib/scheduling.cjs');
const {recommend}=require('../scripts/scheduling.cjs');
const MODEL='legacy-fixture/model-a',NATIVE='native-fixture',OTHER='native-secondary';
const binding={revision:'a'.repeat(64),model:'upstream-model-a',connection:'connection-a'};
const catalog=()=>({models:{[MODEL]:['high','max'],[NATIVE]:['medium','high'],[OTHER]:['high']},hubModels:{[MODEL]:binding},nativeModels:[NATIVE,OTHER]});
const preferences=()=>Object.fromEntries(scheduling.TASKS.map(task=>[task,{model:MODEL,effort:'high',fallback_model:NATIVE,fallback_effort:'medium'}]));
function local(t){const root=makeTestDir(t,'hub-scheduling-'),before=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
 t.after(()=>{if(before===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=before;});return root;}
function deps(overrides={}){return {loadDisabledModels:()=>new Set(),loadAvailableModels:catalog,
 resolveBindingKind:async()=>({...binding,kind:'bound'}),currentSession:async()=>({}),senderContext:()=>({version:'v1'}),
 parentProvider:()=>null,resolveParentRoute:async()=>'compatible',...overrides};}
test('saved preferences drive manual selection; explicit requests never silently fall back',t=>{
 local(t);const prefs=preferences();scheduling.saveManualRouting(prefs);
 assert.equal(scheduling.select('bounded',scheduling.readPreferences(),catalog(),null,{mode:'manual'}).model,MODEL);
 prefs.bounded.model=NATIVE;prefs.bounded.effort='medium';scheduling.saveRoutingPreferences(prefs);
 assert.equal(scheduling.select('bounded',scheduling.readPreferences(),catalog(),null,{mode:'manual'}).model,NATIVE);
 prefs.boundary.fallback_model=OTHER;prefs.boundary.fallback_effort='high';scheduling.saveRoutingPreferences(prefs);
 assert.equal(scheduling.readPreferences().boundary.fallback_model,OTHER);
 const available={models:{[NATIVE]:['medium']},nativeModels:[NATIVE]};
 assert.equal(scheduling.select('bounded',scheduling.readPreferences(),available,null,{mode:'manual'}).model,NATIVE);
 const explicit=scheduling.select('bounded',scheduling.readPreferences(),available,{model:MODEL,effort:'high'});
 assert.equal(explicit.code,'EXPLICIT_MODEL_UNAVAILABLE');
 prefs.bounded.model=MODEL;prefs.bounded.effort='invalid';assert.throws(()=>scheduling.saveRoutingPreferences(prefs),/不符合/);
 assert.equal(scheduling.readPreferences().bounded.model,NATIVE);
});
test('disabled and re-enabled bindings recalculate without probing the disabled model',async t=>{
 local(t);scheduling.saveManualRouting(preferences());let disabled=true,reads=0;
 const d=deps({loadDisabledModels:()=>new Set(disabled?[MODEL]:[]),resolveBindingKind:async()=>{reads++;return {...binding,kind:'bound'};}});
 let result=await recommend('bounded',null,d);assert.equal(result.model,NATIVE);assert.equal(result.source,'fallback');assert.equal(reads,0);
 result=await recommend('bounded',{model:MODEL,effort:'high'},d);assert.equal(result.ok,false);assert.equal(reads,0);
 disabled=false;result=await recommend('bounded',null,d);assert.equal(result.model,MODEL);assert.equal(result.dispatch_status,'route_checked');assert.equal(reads,2);
 assert.deepEqual(scheduling.readPreferences(),preferences());
});
test('binding changes during route proof select the saved fallback',async t=>{
 local(t);scheduling.saveManualRouting(preferences());let reads=0;
 const d=deps({resolveBindingKind:async()=>({...binding,kind:'bound',revision:(++reads===1?'a':'b').repeat(64)})});
 const result=await recommend('bounded',null,d);assert.equal(result.model,NATIVE);
 assert.ok(result.skipped.some(item=>item.code==='HUB_BINDING_CHANGED'));
});
test('automatic selection uses remaining registered models without rewriting preferences',t=>{
 local(t);const prefs=preferences(),available={models:{[OTHER]:['high'],[NATIVE]:['medium']},nativeModels:[OTHER,NATIVE]};
 const critical=scheduling.select('critical',prefs,available,null,{mode:'auto',preferencesSaved:true});
 assert.equal(critical.model,NATIVE);assert.equal(critical.source,'automatic');
 const remaining=scheduling.select('bounded',prefs,{models:{[OTHER]:['high']},nativeModels:[OTHER]},null,{mode:'auto',preferencesSaved:true});
 assert.equal(remaining.model,OTHER);assert.deepEqual(prefs,preferences());
 const explicit=scheduling.select('bounded',prefs,available,{model:MODEL,effort:'high'});assert.equal(explicit.code,'EXPLICIT_MODEL_UNAVAILABLE');
});
test('bound model without a current session remains pending without parent route check',async t=>{
 local(t);const d=deps({currentSession:async()=>null,resolveParentRoute:async()=>{throw Error('unexpected probe');}});
 const result=await recommend('bounded',{model:MODEL,effort:'high'},d);
 assert.equal(result.model,MODEL);assert.equal(result.dispatch_status,'pending_dispatch');assert.equal(result.dispatch_code,'HUB_DISPATCH_CHECK_PENDING');
});
