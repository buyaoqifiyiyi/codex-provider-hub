'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {DEFAULTS,select,readRoutingMode,readRoutingSnapshot,saveRoutingMode,saveManualRouting,saveRoutingPreferences,savePreferences,readPreferences}=require('../lib/scheduling.cjs');

const A='hub-11111111111141118111111111111111/model-a';
const B='hub-22222222222242228222222222222222/model-b';
const NATIVE='native-fixture';
const ROUTE='fixture-route/model-route';
const binding=model=>({revision:'a'.repeat(64),model,connection:model});
const available={models:{[A]:['high'],[B]:['high'],[NATIVE]:['high']},
  hubModels:{[A]:binding('model-a'),[B]:binding('model-b')},nativeModels:[NATIVE,'native-basic','native-backup']};
const prefs={bounded:{model:A,effort:'high',fallback_model:NATIVE,fallback_effort:'high'},
  boundary:{model:A,effort:'high',fallback_model:NATIVE,fallback_effort:'high'},
  critical:{model:NATIVE,effort:'high',fallback_model:A,fallback_effort:'high'}};
const sample={purpose:'tool',channel:'subagent_gateway',effort:'high',status:'passed',tested_at:'2026-10-30T00:00:00Z'};
const comparison=(score,conditions='a'.repeat(64),benchmark='terminal-bench 2.1')=>({status:'available',results:[
  {benchmark,task_id:'terminalbench_2_1',unit:'%',conditions_fingerprint:conditions,score,verified:false}]});

test('auto uses saved task preferences and exact strengths when evidence is not comparable',()=>{
  const manual={
    bounded:{model:ROUTE,effort:'high',fallback_model:'native-basic',fallback_effort:'xhigh'},
    boundary:{model:ROUTE,effort:'max',fallback_model:NATIVE,fallback_effort:'high'},
    critical:{model:NATIVE,effort:'high',fallback_model:'native-backup',fallback_effort:'high'}
  };
  const models={models:{[ROUTE]:['high','max'],'native-basic':['xhigh'],[NATIVE]:['high'],'native-backup':['high']},
    hubModels:{[ROUTE]:binding('model-route')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const refs={[ROUTE]:{status:'available',results:[{benchmark:'cais/hle',score:42.6}]}};
  for (const [task,expected,effort] of [['bounded',ROUTE,'high'],['boundary',ROUTE,'max'],['critical',NATIVE,'high']]) {
    const r=select(task,manual,models,null,{mode:'auto',preferencesSaved:true,purpose:'text',references:refs});
    assert.equal(r.model,expected);assert.equal(r.effort,effort);
    assert.ok(r.selection_reasons.some(x=>x.includes('首选')));
  }
});

test('auto can use saved fallback and keeps its exact effort when primary is unsuitable',()=>{
  const manual=structuredClone(DEFAULTS);
  manual.boundary={model:ROUTE,effort:'max',fallback_model:NATIVE,fallback_effort:'high'};
  const models={models:{[ROUTE]:['high','max'],[NATIVE]:['high'],[A]:['high']},
    hubModels:{[ROUTE]:binding('model-route'),[A]:binding('model-a')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const bad={purpose:'tool',channel:'subagent_gateway',effort:'max',status:'failed',tested_at:'2026-10-30T01:00:00Z'};
  const r=select('boundary',manual,models,null,{mode:'auto',preferencesSaved:true,purpose:'tool',evidence:{[ROUTE]:[bad]}});
  assert.equal(r.model,NATIVE);assert.equal(r.effort,'high');
  assert.ok(r.selection_reasons.some(x=>x.includes('备选')));
});

test('the same model uses its saved primary strength before its fallback strength',()=>{
  const manual=structuredClone(DEFAULTS);
  manual.boundary={model:ROUTE,effort:'max',fallback_model:ROUTE,fallback_effort:'high'};
  const models={models:{[ROUTE]:['high','max']},hubModels:{[ROUTE]:binding('model-route')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const result=select('boundary',manual,models,null,{mode:'auto',preferencesSaved:true,purpose:'tool',evidence:{[ROUTE]:[sample]}});
  assert.equal(result.model,ROUTE);assert.equal(result.effort,'max');
});

test('auto without user-saved preferences does not treat displayed defaults as user choices',()=>{
  const first=structuredClone(DEFAULTS),second=structuredClone(DEFAULTS);
  first.critical={model:A,effort:'high',fallback_model:B,fallback_effort:'high'};
  second.critical={model:B,effort:'high',fallback_model:A,fallback_effort:'high'};
  const opts={mode:'auto',preferencesSaved:false,purpose:'text'};
  const models={models:{[A]:['high'],[B]:['high'],[NATIVE]:['high']},hubModels:{[A]:binding('model-a'),[B]:binding('model-b')},nativeModels:[NATIVE,'native-basic','native-backup']};
  assert.equal(select('critical',first,models,null,opts).model,select('critical',second,models,null,opts).model);
});

test('manual mode never silently substitutes an unconfigured third choice',()=>{
  const manual=structuredClone(DEFAULTS);
  manual.bounded={model:A,effort:'high',fallback_model:B,fallback_effort:'high'};
  const models={models:{[NATIVE]:['high']},hubModels:{},nativeModels:[NATIVE,'native-basic','native-backup']};
  const r=select('bounded',manual,models,null,{mode:'manual',preferencesSaved:true});
  assert.equal(r.ok,false);assert.equal(r.code,'NO_ELIGIBLE_MODEL');
});

test('a comparable stronger candidate may supersede an auto preference without altering it',()=>{
  const manual=structuredClone(DEFAULTS);
  manual.bounded={model:A,effort:'high',fallback_model:NATIVE,fallback_effort:'high'};
  const models={models:{[A]:['high'],[B]:['high']},hubModels:{[A]:binding('model-a'),[B]:binding('model-b')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const r=select('bounded',manual,models,null,{mode:'auto',preferencesSaved:true,purpose:'tool',
    evidence:{[A]:[sample],[B]:[sample]},references:{[A]:comparison(40),[B]:comparison(90)}});
  assert.equal(r.model,B);assert.equal(manual.bounded.model,A);
  assert.ok(r.selection_reasons.some(x=>x.includes('可比')));
});

test('automatic is the default and switching modes preserves old preferences',t=>{
  const root=makeTestDir(t,'hub-auto-mode-');
  const old=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
  t.after(()=>{if(old===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=old;});
  savePreferences(prefs);
  assert.equal(readRoutingMode(),'auto');
  assert.equal(saveRoutingMode('manual'),'manual');
  assert.equal(readRoutingMode(),'manual');
  assert.equal(saveRoutingMode('auto'),'auto');
  assert.equal(readPreferences().bounded.model,A);
  assert.throws(()=>saveRoutingMode('unknown'),/无效/);
  assert.equal(readRoutingMode(),'auto');
});

test('manual preference and mode are saved together without changing legacy backup',t=>{
  const root=makeTestDir(t,'hub-auto-atomic-');
  const old=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
  t.after(()=>{if(old===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=old;});
  savePreferences(prefs);
  const legacy=fs.readFileSync(path.join(root,'CodexProviderHub','scheduling.json'),'utf8');
  const replacement=structuredClone(prefs);replacement.bounded.model=B;
  saveManualRouting(replacement);
  assert.equal(readRoutingMode(),'manual');assert.equal(readPreferences().bounded.model,B);
  assert.equal(fs.readFileSync(path.join(root,'CodexProviderHub','scheduling.json'),'utf8'),legacy);
  saveRoutingMode('auto');assert.equal(readPreferences().bounded.model,B);
  const invalid=structuredClone(prefs);invalid.bounded.effort='invalid';
  assert.throws(()=>saveManualRouting(invalid),/不符合/);
  assert.equal(readRoutingMode(),'auto');assert.equal(readPreferences().bounded.model,B);
});

test('saving preferences retains the active mode and unsaved installations stay automatic',t=>{
  const root=makeTestDir(t,'hub-auto-save-');
  const old=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
  t.after(()=>{if(old===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=old;});
  assert.equal(readRoutingSnapshot().saved,false);
  assert.equal(readRoutingMode(),'auto');
  assert.throws(()=>saveRoutingMode('manual'),{code:'SCHEDULING_PREFERENCES_REQUIRED'});
  assert.equal(readRoutingMode(),'auto');
  const saved=saveRoutingPreferences(prefs);
  assert.equal(saved.mode,'auto');assert.equal(readRoutingSnapshot().saved,true);
  assert.equal(saveRoutingMode('manual'),'manual');
  const updated=structuredClone(prefs);updated.bounded.model=B;
  assert.equal(saveRoutingPreferences(updated).mode,'manual');
  assert.equal(readPreferences().bounded.model,B);
  assert.equal(saveRoutingMode('auto'),'auto');
  assert.equal(readPreferences().bounded.model,B);
});

test('automatic selection compares genuinely matching task results instead of fixing the saved preference',()=>{
  const options={mode:'auto',purpose:'tool',evidence:{[A]:[sample],[B]:[sample]},
    references:{[A]:comparison(50),[B]:comparison(90)}};
  const selected=select('bounded',prefs,available,null,options);
  assert.equal(selected.model,B);
  assert.equal(selected.source,'automatic');
  assert.equal(selected.effort,'high');
  assert.ok(selected.selection_reasons.some(x=>x.includes('评测')));
  assert.equal(prefs.bounded.model,A); // Existing manual preferences are not overwritten.
});

test('missing benchmarks preserve saved native preference while other tasks can select a binding',()=>{
  const models={models:{[ROUTE]:['high','max'],[NATIVE]:['high']},
    hubModels:{[ROUTE]:binding('model-route')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const references={[ROUTE]:{status:'available',results:[{benchmark:'cais/hle',score:42.6}]}};
  const evidence={[ROUTE]:[sample]};
  const selectedPrefs=structuredClone(prefs);selectedPrefs.bounded={model:ROUTE,effort:'high',fallback_model:NATIVE,fallback_effort:'high'};
  for (const purpose of ['text','tool']) {
    const result=select('critical',selectedPrefs,models,null,{mode:'auto',preferencesSaved:true,purpose,references,evidence});
    assert.equal(result.model,NATIVE);
    assert.equal(result.effort,'high');
  }
  assert.equal(select('bounded',selectedPrefs,models,null,{mode:'auto',preferencesSaved:true,purpose:'tool',references,evidence}).model,ROUTE);
});

test('a newer enabled model can still win critical work when results are truly comparable',()=>{
  const models={models:{[ROUTE]:['high'],[NATIVE]:['high']},hubModels:{[ROUTE]:binding('model-route')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const hle=(score)=>({status:'available',results:[{benchmark:'cais/hle',task_id:'hle',unit:'%',
    conditions_fingerprint:'c'.repeat(64),score,verified:false}]});
  const result=select('critical',prefs,models,null,{mode:'auto',preferencesSaved:true,purpose:'text',
    references:{[ROUTE]:hle(90),[NATIVE]:hle(50)}});
  assert.equal(result.model,ROUTE);
  assert.ok(result.selection_reasons.some(x=>x.includes('可比')));
});

test('different benchmark versions or test conditions are not compared as scores',()=>{
  for (const different of [comparison(90,'b'.repeat(64)),comparison(90,'a'.repeat(64),'terminal-bench 4')]) {
    const opt={mode:'auto',purpose:'tool',evidence:{[A]:[sample],[B]:[sample]},
      references:{[A]:comparison(50),[B]:different}};
    const initial=select('bounded',prefs,available,null,opt);
    opt.references[A].results[0].score=99; opt.references[B].results[0].score=1;
    const swapped=select('bounded',prefs,available,null,opt);
    assert.equal(swapped.model,initial.model);
    assert.ok(!initial.selection_reasons.some(x=>x.includes('可比')));
  }
});

test('an unrelated weak candidate cannot create a ranking across incomparable benchmarks',()=>{
  const C='hub-33333333333343338333333333333333/model-c';
  const base={models:{[A]:['high'],[C]:['high']},hubModels:{[A]:binding('model-a'),[C]:binding('model-c')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const opts={mode:'auto',purpose:'tool',evidence:{[A]:[sample],[B]:[sample],[C]:[sample]},
    references:{[A]:comparison(50,'a'.repeat(64),'terminal-bench 2.1'),
                [B]:comparison(10,'b'.repeat(64),'terminal-bench 4'),
                [C]:comparison(90,'b'.repeat(64),'terminal-bench 4')}};
  const before=select('bounded',prefs,base,null,opts);
  const withThird={models:{...base.models,[B]:['high']},hubModels:{...base.hubModels,[B]:binding('model-b')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const after=select('bounded',prefs,withThird,null,opts);
  assert.equal(before.model,A);assert.equal(after.model,A);
  assert.ok(!after.selection_reasons.some(x=>x.includes('可比')));
});

test('conflicting comparable benchmarks do not turn into an arbitrary global winner',()=>{
  const a=comparison(90),b=comparison(40);
  a.results.push({...comparison(30,'b'.repeat(64),'swe-bench pro').results[0],task_id:'swe-pro'});
  b.results.push({...comparison(90,'b'.repeat(64),'swe-bench pro').results[0],task_id:'swe-pro'});
  const opts={mode:'auto',purpose:'tool',evidence:{[A]:[sample],[B]:[sample]},references:{[A]:a,[B]:b}};
  const two={models:{[A]:['high'],[B]:['high']},hubModels:{[A]:binding('model-a'),[B]:binding('model-b')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const result=select('bounded',prefs,two,null,opts);
  assert.equal(result.model,A);
  assert.ok(!result.selection_reasons.some(x=>x.includes('可比')));
});

test('a third candidate with missing results cannot hide two models contradictory results',()=>{
  const C='hub-33333333333343338333333333333333/model-c';
  const first=comparison(50),second=comparison(90),third=comparison(20);
  first.results.push({...comparison(90,'b'.repeat(64),'swe-bench pro').results[0],task_id:'swe-pro'});
  second.results.push({...comparison(40,'b'.repeat(64),'swe-bench pro').results[0],task_id:'swe-pro'});
  const manual=structuredClone(DEFAULTS);manual.bounded={model:A,effort:'high',fallback_model:B,fallback_effort:'high'};
  const models={models:{[A]:['high'],[B]:['high'],[C]:['high']},
    hubModels:{[A]:binding('model-a'),[B]:binding('model-b'),[C]:binding('model-c')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const result=select('bounded',manual,models,null,{mode:'auto',preferencesSaved:true,purpose:'tool',
    evidence:{[A]:[sample],[B]:[sample],[C]:[sample]},references:{[A]:first,[B]:second,[C]:third}});
  assert.equal(result.model,A);
  assert.ok(!result.selection_reasons.some(x=>x.includes('可比')));
});

test('explicit strength follows catalog unless a user policy restricts it',t=>{
  const models={models:{[NATIVE]:['low','high'],[ROUTE]:['high','max']},hubModels:{[ROUTE]:binding('model-route')},nativeModels:[NATIVE,'native-basic','native-backup']};
  const result=select('critical',prefs,models,null,{mode:'auto',preferencesSaved:false,purpose:'text',requiredEffort:'low'});
  assert.equal(result.model,NATIVE);assert.equal(result.effort,'low');
  const boundCatalog={models:{[ROUTE]:['low','high']},hubModels:{[ROUTE]:binding('model-route')}};
  assert.equal(select('critical',prefs,boundCatalog,null,{mode:'auto',preferencesSaved:false,requiredEffort:'low'}).model,ROUTE);
  const root=makeTestDir(t,'synthetic-user-policy-');
  const previous=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
  t.after(()=>{if(previous===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=previous;});
  fs.mkdirSync(path.join(root,'CodexProviderHub'));
  fs.writeFileSync(path.join(root,'CodexProviderHub','model-policy.json'),JSON.stringify({schemaVersion:2,allowedFamilies:[],allowedEffortsByFamily:{'model-route':['high']},jevAdvisory:{enabled:false}}));
  const forbidden=select('critical',prefs,boundCatalog,null,
    {mode:'auto',preferencesSaved:false,purpose:'text',requiredEffort:'low'});
  assert.equal(forbidden.ok,false);
});

test('auto selection respects explicit user choices and never invents a fallback',()=>{
  const options={mode:'auto',purpose:'tool',evidence:{[A]:[sample],[B]:[sample]},references:{[A]:comparison(20),[B]:comparison(90)}};
  assert.equal(select('bounded',prefs,available,{model:A,effort:'high'},options).model,A);
  const absent=select('bounded',prefs,{models:{[B]:['high']},hubModels:{[B]:binding('model-b')},nativeModels:[NATIVE,'native-basic','native-backup']},{model:A,effort:'high'},options);
  assert.equal(absent.ok,false);assert.equal(absent.code,'EXPLICIT_MODEL_UNAVAILABLE');
});

test('image-relevant public information helps select a candidate without treating missing data as failure',()=>{
  const options={mode:'auto',purpose:'image',references:{[B]:{status:'available',results:[
    {benchmark:'CharXiv (Multimodal)',score:86.2,unit:'%',verified:null}]}}};
  const selected=select('bounded',prefs,available,null,options);
  assert.equal(selected.model,B);
  assert.ok(selected.selection_reasons.some(x=>x.includes('图片')));
  assert.equal(select('bounded',prefs,{models:{[A]:['high']},hubModels:{[A]:binding('model-a')},nativeModels:[NATIVE,'native-basic','native-backup']},null,options).model,A);
});

test('latest failed purpose sample is skipped in auto mode and stale evidence does not win',()=>{
  const failed={...sample,status:'failed',tested_at:'2026-10-30T01:00:00Z'};
  const options={mode:'auto',purpose:'tool',evidence:{[A]:[sample,failed],[B]:[sample]}};
  assert.equal(select('bounded',prefs,available,null,options).model,B);
  const removed={models:{[NATIVE]:['high']},hubModels:{},nativeModels:[NATIVE,'native-basic','native-backup']};
  assert.equal(select('bounded',prefs,removed,null,options).model,NATIVE);
  const stale={...sample,status:'stale',tested_at:'2026-10-30T02:00:00Z'};
  const one={models:{[A]:['high']},hubModels:{[A]:binding('model-a')},nativeModels:[NATIVE,'native-basic','native-backup']};
  assert.equal(select('bounded',prefs,one,null,{mode:'auto',purpose:'tool',evidence:{[A]:[sample,stale]}}).purpose_status,'sample_passed');
});
