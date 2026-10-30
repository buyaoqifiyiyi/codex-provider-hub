'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {makeTestDir} = require('./safe-temp-fixture.cjs');
const scheduling = require('../lib/scheduling.cjs');
const {validateSpawn, eligibleModels, hubBinding, loadPolicy} = require('../lib/model-policy.cjs');
const policy = require('../policy.json');
const {persistence} = require('../lib/state-persistence.cjs');
// The whole suite runs outside the user's actual policy and persistence paths.
const originalLocalAppData=process.env.LOCALAPPDATA;
const suiteLocalAppData=makeTestDir(test,'neutral-policy-suite-');
process.env.LOCALAPPDATA=suiteLocalAppData;
test.after(()=>{
  if(originalLocalAppData===undefined) delete process.env.LOCALAPPDATA;
  else process.env.LOCALAPPDATA=originalLocalAppData;
});
const A = 'Vendor/Team/Any-Model';
const B = 'opaque-old-alias';
const binding = model => ({revision:'a'.repeat(64),model,connection:'synthetic-only'});
const catalog = (entries = {[A]:['minimal','high'],[B]:['low','max']}) => ({
  models:entries,hubModels:Object.fromEntries(Object.keys(entries).map(id=>[id,binding(id)]))
});
const preferences = (model=A,effort='minimal',fallback_model=B,fallback_effort='low') =>
  Object.fromEntries(scheduling.TASKS.map(task=>[task,{model,effort,fallback_model,fallback_effort}]));
const auto = (available, options={}, prefs=scheduling.DEFAULTS, task='critical') =>
  scheduling.select(task,prefs,available,null,{mode:'auto',preferencesSaved:false,...options});
const sample = (effort,status='passed',purpose='tool',tested_at='2026-10-30T00:00:00Z') =>
  ({effort,status,purpose,tested_at,channel:'subagent_gateway'});

test('no saved preference and no old lines: every task chooses only actual candidates',()=>{
  assert.deepEqual(policy.allowedFamilies,[]);
  for (const task of scheduling.TASKS) {
    assert.deepEqual(scheduling.DEFAULTS[task],{model:'',effort:'',fallback_model:'',fallback_effort:''});
    const r=auto(catalog(),{},undefined,task);
    assert.equal(r.model,A);assert.equal(r.effort,'minimal');assert.equal(r.purpose_status,'pending_test');
  }
  assert.equal(auto(catalog({})).code,'NO_ELIGIBLE_MODEL');
});

test('renaming models and providers preserves candidate-order decisions',()=>{
  for (const id of ['Unseen/Model-Q','host-family-sol','fixture-model','legacy-route/fixture-model']) {
    const c=catalog({[id]:['low']});
    assert.equal(auto(c).model,id);assert.equal(auto(c).effort,'low');
    assert.equal(hubBinding(c,id),c.hubModels[id]);
    delete c.hubModels[id];
    assert.equal(auto(c).ok,false);assert.equal(hubBinding(c,id),null);
  }
});

test('catalog alone and historical allowed family names cannot authorize models',()=>{
  const model='host-family-sol';
  for (const p of [policy,{schemaVersion:2,allowedFamilies:['host-family']}]) {
    assert.equal(validateSpawn({model,reasoning_effort:'high'},p,{models:{[model]:['high']}}).code,'MODEL_NOT_ALLOWED');
  }
  const c={models:{[A]:['none']},managedOfficialModels:[A]};
  assert.equal(auto(c).model,A);
  c.hubModels={[A]:{...binding(A),revision:'invalid'}};
  assert.equal(auto(c).ok,false);
});

test('binding authenticity, disabled flags, removed catalog rows fail closed',()=>{
  for (const broken of [{revision:'bad'},{connection:''},{model:''},{enabled:false},{disabled:true},{kind:'disabled'}]) {
    const c=catalog();c.hubModels[A]={...binding(A),...broken};
    assert.equal(hubBinding(c,A),null);assert.ok(!Object.hasOwn(eligibleModels(policy,c),A));
  }
  for (const disabledModels of [[A],new Set([A])]) {
    const c={...catalog(),disabledModels};assert.equal(auto(c).model,B);assert.equal(hubBinding(c,A),null);
  }
  const c=catalog();delete c.models[A];assert.equal(hubBinding(c,A),null);assert.equal(auto(c).model,B);
});

test('every host effort is validated against the directory without model restrictions',()=>{
  const levels=['none','minimal','low','medium','high','xhigh','max','ultra'];
  for (const model of [A,'legacy-route/fixture-model']) {
    const c=catalog({[model]:levels});
    for (const effort of levels) {
      assert.equal(validateSpawn({model,reasoning_effort:effort},policy,c).ok,true);
      assert.equal(auto(c,{requiredEffort:effort}).effort,effort);
    }
    c.models[model]=['minimal'];
    assert.equal(validateSpawn({model,reasoning_effort:'max'},policy,c).code,'MODEL_EFFORT_UNSUPPORTED');
    assert.equal(auto(c,{requiredEffort:'max'}).ok,false);
  }
});

test('unknown effort and unknown purpose capabilities are never reported as supported',()=>{
  const c=catalog({[A]:[]});assert.equal(auto(c).ok,false);
  assert.equal(validateSpawn({model:A,reasoning_effort:'high'},policy,c).code,'MODEL_EFFORT_UNSUPPORTED');
  assert.equal(scheduling.capability(A,c).critical_eligible,false);
  const r=auto(catalog(),{purpose:'image'});
  assert.equal(r.purpose_status,'pending_test');
  assert.equal(scheduling.capability(r.model,catalog()).capability,'unknown');
});

test('saved automatic preferences keep exact strengths without mutating data',()=>{
  const prefs=preferences(B,'max',A,'high'),before=structuredClone(prefs);
  for (const task of scheduling.TASKS) {
    const r=auto(catalog(),{preferencesSaved:true},prefs,task);
    assert.equal(r.model,B);assert.equal(r.effort,'max');
  }
  assert.deepEqual(prefs,before);
  const removed=catalog({[A]:['minimal','high']});
  const r=auto(removed,{preferencesSaved:true},prefs);
  assert.equal(r.model,A);assert.equal(r.effort,'high');
});

test('unsaved display choices do not become user preferences; omitted mode stays automatic',()=>{
  const prefs=preferences(B,'max',A,'high');
  assert.equal(auto(catalog(),{},prefs).model,A);
  const r=scheduling.select('bounded',scheduling.DEFAULTS,catalog());
  assert.equal(r.model,A);assert.equal(r.source,'automatic');
});

test('manual selects only saved primary or fallback, even with strong third-party evidence',()=>{
  const prefs=preferences(), c=catalog({[B]:['low'],third:['high']});
  const opts={mode:'manual',preferencesSaved:true,purpose:'tool',evidence:{third:[sample('high')]}};
  let r=scheduling.select('critical',prefs,c,null,opts);
  assert.equal(r.model,B);assert.equal(r.source,'fallback');assert.equal(r.effort,'low');
  delete c.models[B];
  assert.equal(scheduling.select('critical',prefs,c,null,opts).code,'NO_ELIGIBLE_MODEL');
  assert.equal(scheduling.select('critical',scheduling.DEFAULTS,c,null,opts).ok,false);
});

test('explicit model and effort never silently change or fall back',()=>{
  const prefs=preferences();
  for (const mode of ['manual','auto']) {
    const r=scheduling.select('critical',prefs,catalog(),{model:B,effort:'low'},{mode});
    assert.equal(r.model,B);assert.equal(r.effort,'low');assert.equal(r.source,'explicit');
    const unavailable=scheduling.select('critical',prefs,catalog(),{model:B,effort:'high'},{mode});
    assert.equal(unavailable.code,'EXPLICIT_MODEL_UNAVAILABLE');
  }
});

test('purpose evidence ranks candidates and uses only the latest matching effort/channel',()=>{
  const evidence={[B]:[sample('max')]};
  let r=auto(catalog(),{purpose:'tool',evidence});assert.equal(r.model,B);assert.equal(r.effort,'max');
  evidence[B].push(sample('max','failed','tool','2026-10-30T01:00:00Z'));
  r=auto(catalog(),{purpose:'tool',evidence});assert.equal(r.model,A);
  const excluded=[{model:A,effort:'minimal'},{model:A,effort:'high'},{model:B,effort:'low'}];
  assert.equal(auto(catalog(),{purpose:'tool',evidence,excluded}).ok,false);
  evidence[B]=[{...sample('max'),channel:'other'}];
  assert.equal(auto(catalog(),{purpose:'tool',evidence}).purpose_status,'pending_test');
});

test('comparable benchmarks can inform auto selection without claiming runtime support',()=>{
  const metric=score=>({status:'available',results:[{benchmark:'hle',task_id:'same',unit:'%',conditions_fingerprint:'c'.repeat(64),score}]});
  const references={[A]:metric(20),[B]:metric(90)};
  const r=auto(catalog(),{references});assert.equal(r.model,B);assert.equal(r.purpose_status,'pending_test');
  references[B].results[0].conditions_fingerprint='d'.repeat(64);
  assert.equal(auto(catalog(),{references}).model,A);
});

test('legacy and current persistence preserve saved low/minimal preferences and modes',t=>{
  const root=makeTestDir(t,'neutral-scheduling-');
  const old=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
  t.after(()=>{if(old===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=old;});
  assert.equal(scheduling.readRoutingSnapshot().saved,false);
  assert.deepEqual(scheduling.readPreferences(),scheduling.DEFAULTS);
  assert.throws(()=>scheduling.saveRoutingMode('manual'),{code:'SCHEDULING_PREFERENCES_REQUIRED'});
  const prefs=preferences('legacy-route/fixture-model','low',A,'minimal');
  scheduling.savePreferences(prefs);assert.deepEqual(scheduling.readPreferences(),prefs);
  scheduling.saveRoutingMode('manual');assert.deepEqual(scheduling.readRoutingSnapshot(),{mode:'manual',preferences:prefs,saved:true});
  scheduling.saveRoutingMode('auto');assert.deepEqual(scheduling.readPreferences(),prefs);
  scheduling.saveManualRouting(prefs);assert.deepEqual(scheduling.readPreferences(),prefs);
  const changed=preferences(B,'max',A,'none');scheduling.saveRoutingPreferences(changed);
  assert.equal(scheduling.readRoutingMode(),'manual');assert.deepEqual(scheduling.readPreferences(),changed);
  const invalid=structuredClone(prefs);invalid.critical.effort='invented';
  assert.throws(()=>scheduling.saveManualRouting(invalid));
  assert.throws(()=>persistence('manual',{root:path.join(root,'isolated'),preferences:invalid}));
  assert.deepEqual(scheduling.readPreferences(),changed);
});

test('guard preflight receives exact binding metadata for arbitrary and legacy aliases',()=>{
  const guard=require('../lib/guard.cjs');
  for (const id of [A,'legacy-route/fixture-model']) {
    const c=catalog({[id]:['low']});
    const r=guard.validateSpawn({model:id,reasoning_effort:'low'},{policy,availableModels:c,disabledModels:new Set()});
    assert.equal(r.ok,true);assert.deepEqual(r.hubBinding,c.hubModels[id]);
  }
});

test('explicit policy data can restrict effort but invalid policies never authorize',()=>{
  const c=catalog({[A]:['minimal','high']});
  const restricted={schemaVersion:2,allowedFamilies:[A],allowedEffortsByFamily:{[A]:['high']}};
  assert.deepEqual(eligibleModels(restricted,c)[A],['high']);
  assert.equal(validateSpawn({model:A,reasoning_effort:'minimal'},restricted,c).code,'MODEL_EFFORT_NOT_ALLOWED');
  assert.equal(validateSpawn({model:A,reasoning_effort:'high'},{...policy,schemaVersion:999},c).code,'MODEL_POLICY_INVALID');
});

test('trusted nativeModels authorizes arbitrary native IDs with no managed subscription records',()=>{
  const native='Native/Future-Engine';
  const c={models:{[native]:['minimal','high'],unidentified:['high']},nativeModels:[native],managedOfficialModels:[]};
  assert.deepEqual(eligibleModels(policy,c),{[native]:['minimal','high']});
  assert.equal(validateSpawn({model:native,reasoning_effort:'minimal'},policy,c).ok,true);
  assert.equal(auto(c).model,native);
  assert.equal(scheduling.select('critical',preferences(native,'high'),c,null,{mode:'manual'}).model,native);
  assert.equal(validateSpawn({model:'unidentified',reasoning_effort:'high'},policy,c).code,'MODEL_NOT_ALLOWED');
  const guard=require('../lib/guard.cjs');
  const r=guard.validateSpawn({model:native,reasoning_effort:'high'},{policy,availableModels:c,disabledModels:new Set()});
  assert.equal(r.ok,true);assert.equal(Object.hasOwn(r,'hubBinding'),false);
});

test('native identity never bypasses a forged or disabled binding for the same ID',()=>{
  for(const broken of [null,{}, {...binding(A),revision:'forged'}, {...binding(A),connection:''},
    {...binding(A),enabled:false},{...binding(A),disabled:true},{...binding(A),kind:'disabled'}]) {
    const c={models:{[A]:['high']},nativeModels:[A],managedOfficialModels:[A],hubModels:{[A]:broken}};
    assert.equal(validateSpawn({model:A,reasoning_effort:'high'},policy,c).code,'MODEL_NOT_ALLOWED');
    assert.deepEqual(eligibleModels(policy,c),{});assert.equal(auto(c).ok,false);
  }
  for(const disabledModels of [[A],new Set([A])]) {
    const c={models:{[A]:['high']},nativeModels:[A],managedOfficialModels:[],disabledModels};
    assert.equal(validateSpawn({model:A,reasoning_effort:'high'},policy,c).code,'MODEL_NOT_ALLOWED');
    assert.equal(auto(c).ok,false);
  }
});

test('native identity requires exact list membership, current catalog presence and supported effort',()=>{
  for(const nativeModels of [undefined,A,{[A]:true},[],[A+'-other']]) {
    const c={models:{[A]:['high']},nativeModels,managedOfficialModels:[]};
    assert.equal(validateSpawn({model:A,reasoning_effort:'high'},policy,c).code,'MODEL_NOT_ALLOWED');
  }
  const c={models:{[A]:['minimal']},nativeModels:[A],managedOfficialModels:[]};
  assert.equal(validateSpawn({model:A,reasoning_effort:'high'},policy,c).code,'MODEL_EFFORT_UNSUPPORTED');
  c.models[A]=[];assert.equal(auto(c).ok,false);
  delete c.models[A];
  assert.equal(validateSpawn({model:A,reasoning_effort:'minimal'},policy,c).code,'MODEL_UNAVAILABLE');
  assert.equal(auto(c).ok,false);
});

function policyFixture(t) {
  const previous=process.env.LOCALAPPDATA;
  const root=makeTestDir(t,'neutral-local-policy-');
  const folder=path.join(root,'CodexProviderHub');
  const file=path.join(folder,'model-policy.json');
  fs.mkdirSync(folder);
  process.env.LOCALAPPDATA=root;
  t.after(()=>{
    if(previous===undefined) delete process.env.LOCALAPPDATA;
    else process.env.LOCALAPPDATA=previous;
  });
  return file;
}

test('missing local policy uses neutral product defaults without writing a policy',t=>{
  const file=policyFixture(t);
  assert.deepEqual(loadPolicy(),policy);
  const native='fictional-native-v3';
  const c={models:{[native]:['low']},nativeModels:[native],managedOfficialModels:[]};
  assert.equal(auto(c).model,native);
  assert.equal(fs.existsSync(file),false);
});

test('local family and effort rules affect native IDs and actual bound models independently',t=>{
  const file=policyFixture(t);
  const native='atlas-2-reasoner', unrelated='atlas-2.1-reasoner';
  const legacy='legacy-route/old-model';
  const managed='subscription-other-v1';
  const local={schemaVersion:2,allowedFamilies:['atlas-2'],
    allowedEffortsByFamily:{'atlas-2':['high'],'alt-v1':['low'],[managed]:['medium']},
    jevAdvisory:{enabled:false}};
  const c={models:{[native]:['low','high'],[unrelated]:['high'],[legacy]:['low','high'],[managed]:['medium','high']},
    nativeModels:[native,unrelated,managed],managedOfficialModels:[managed],
    hubModels:{[legacy]:binding('vendor/ALT-v1-fast')}};
  fs.writeFileSync(file,JSON.stringify(local));
  assert.deepEqual(loadPolicy(),local);
  assert.deepEqual(eligibleModels(loadPolicy(),c),{[native]:['high'],[legacy]:['low'],[managed]:['medium']});
  assert.equal(validateSpawn({model:unrelated,reasoning_effort:'high'},loadPolicy(),c).code,'MODEL_NOT_ALLOWED');
  assert.equal(validateSpawn({model:legacy,reasoning_effort:'high'},loadPolicy(),c).code,'MODEL_EFFORT_NOT_ALLOWED');
  assert.equal(validateSpawn({model:legacy,reasoning_effort:'low'},loadPolicy(),c).ok,true);
  assert.equal(scheduling.select('bounded',preferences(native,'high',legacy,'low'),c,null,
    {mode:'manual'}).model,native);
  assert.equal(scheduling.select('bounded',preferences(unrelated,'high',legacy,'low'),c,null,
    {mode:'manual'}).model,legacy);
  assert.equal(scheduling.select('bounded',preferences(),c,{model:unrelated,effort:'high'}).ok,false);
  assert.equal(scheduling.capability(unrelated,c).critical_eligible,false);
});

test('family matching respects configured prefix boundaries rather than a model name table',t=>{
  const file=policyFixture(t);
  const c={models:{'example-9-next':['low'],'example-9.1-next':['low'],'group/custom-v3':['low'],
    'group/customary-v3':['low'],'other/example-9-next':['low']},
    nativeModels:['example-9-next','example-9.1-next','group/custom-v3','group/customary-v3','other/example-9-next']};
  const local={schemaVersion:2,allowedFamilies:['example-9','group/custom-'],allowedEffortsByFamily:{},jevAdvisory:{enabled:true}};
  fs.writeFileSync(file,JSON.stringify(local));
  assert.deepEqual(Object.keys(eligibleModels(loadPolicy(),c)),['example-9-next','group/custom-v3']);
});

test('overlapping effort rules intersect without changing enabled identities',t=>{
  const file=policyFixture(t);
  const local={schemaVersion:2,allowedFamilies:[],
    allowedEffortsByFamily:{'generic':['high'],'generic-v2':['medium']},jevAdvisory:{enabled:true}};
  const c=catalog({'generic-v1':['high','medium'],'generic-v2':['high','medium']});
  fs.writeFileSync(file,JSON.stringify(local));
  assert.deepEqual(eligibleModels(loadPolicy(),c),{'generic-v1':['high']});
  assert.equal(validateSpawn({model:'generic-v2',reasoning_effort:'high'},loadPolicy(),c).code,'MODEL_EFFORT_NOT_ALLOWED');
});

test('policy reload changes selection immediately without changing saved preferences',t=>{
  const file=policyFixture(t);
  const alpha='alpha-v1',beta='beta-v1';
  const c={models:{[alpha]:['high'],[beta]:['high']},nativeModels:[alpha,beta]};
  const prefs=preferences(alpha,'high',beta,'high');
  scheduling.saveManualRouting(prefs);
  const local={schemaVersion:2,allowedFamilies:[alpha],allowedEffortsByFamily:{},jevAdvisory:{enabled:true}};
  fs.writeFileSync(file,JSON.stringify(local));
  assert.equal(scheduling.select('bounded',scheduling.readPreferences(),c,null,{mode:'manual'}).model,alpha);
  local.allowedFamilies=[beta];fs.writeFileSync(file,JSON.stringify(local));
  assert.equal(scheduling.select('bounded',scheduling.readPreferences(),c,null,{mode:'manual'}).model,beta);
  assert.deepEqual(scheduling.readPreferences(),prefs);
  local.allowedFamilies=[];local.allowedEffortsByFamily={[beta]:['high']};
  fs.writeFileSync(file,JSON.stringify(local));
  assert.equal(scheduling.select('bounded',scheduling.readPreferences(),c,null,{mode:'manual'}).model,alpha);
});

test('invalid or ambiguous local policies fail closed for auto, manual and explicit selection',t=>{
  const file=policyFixture(t);
  const valid={schemaVersion:2,allowedFamilies:['alpha'],allowedEffortsByFamily:{},jevAdvisory:{enabled:true}};
  const cases=[
    '{', 'null', JSON.stringify({...valid,schemaVersion:3}),
    JSON.stringify({...valid,allowedFamilies:null}), JSON.stringify({...valid,allowedFamilies:['']}),
    JSON.stringify({...valid,allowedFamilies:['alpha',42]}), JSON.stringify({...valid,allowedFamilies:['alpha','alpha']}),
    JSON.stringify({...valid,allowedEffortsByFamily:null}), JSON.stringify({...valid,allowedEffortsByFamily:{'alpha':[]}}),
    JSON.stringify({...valid,allowedEffortsByFamily:{'alpha':['invented']}}),
    JSON.stringify({...valid,allowedEffortsByFamily:{'alpha':['high','high']}}),
    JSON.stringify({...valid,jevAdvisory:null}), JSON.stringify({...valid,jevAdvisory:{enabled:'yes'}}),
    JSON.stringify({...valid,jevAdvisory:{enabled:true,unexpected:true}}),
    JSON.stringify({...valid,unexpected:true}),
    JSON.stringify({schemaVersion:2,allowedFamilies:['alpha'],allowedEffortsByFamily:{}}),
    '{"schemaVersion":2,"allowedFamilies":["alpha"],"allowedFamilies":[],"allowedEffortsByFamily":{},"jevAdvisory":{"enabled":true}}',
    '{"schemaVersion":2,"allowedFamilies":["alpha"],"allowedFami\\u006cies":[],"allowedEffortsByFamily":{},"jevAdvisory":{"enabled":true}}',
    '{"schemaVersion":2,"allowedFamilies":[],"allowedEffortsByFamily":{"alpha":["high"],"alpha":["low"]},"jevAdvisory":{"enabled":true}}'
  ];
  const c=catalog({[A]:['high']});
  for(const contents of cases) {
    fs.writeFileSync(file,contents);
    assert.throws(()=>loadPolicy(),{code:'MODEL_POLICY_INVALID'});
    for(const [requested,options] of [[null,{mode:'auto'}],[null,{mode:'manual'}],[{model:A,effort:'high'},{}]]) {
      const r=scheduling.select('critical',preferences(A,'high',A,'high'),c,requested,options);
      assert.equal(r.ok,false);assert.equal(r.code,'MODEL_POLICY_INVALID');
    }
    assert.equal(scheduling.capability(A,c).critical_eligible,false);
  }
});

test('recommendation and guard use the current policy with isolated catalog data',async t=>{
  const file=policyFixture(t);
  const local={schemaVersion:2,allowedFamilies:['permitted'],allowedEffortsByFamily:{},jevAdvisory:{enabled:false}};
  fs.writeFileSync(file,'\ufeff'+JSON.stringify(local));
  const c={models:{'permitted-v1':['high'],'other-v1':['high']},
    nativeModels:['permitted-v1','other-v1'],managedOfficialModels:[],hubModels:{}};
  const {listModels,recommendFromSnapshot}=require('../scripts/list-models.cjs');
  const guard=require('../lib/guard.cjs');
  const snapshot=await listModels({loadDisabledModels:()=>new Set(),loadAvailableModels:()=>c});
  assert.deepEqual(Object.keys(snapshot.models),['permitted-v1']);
  assert.deepEqual(snapshot.nativeModels,c.nativeModels);
  const selected=await recommendFromSnapshot('bounded',scheduling.DEFAULTS,snapshot,null,{},
    {mode:'auto',preferencesSaved:false});
  assert.equal(selected.model,'permitted-v1');
  const options={availableModels:c,disabledModels:new Set()};
  assert.equal(guard.validateSpawn({model:'permitted-v1',reasoning_effort:'high'},options).ok,true);
  assert.equal(guard.validateSpawn({model:'other-v1',reasoning_effort:'high'},options).ok,false);
  fs.writeFileSync(file,'{broken');
  assert.equal(guard.validateSpawn({model:'permitted-v1',reasoning_effort:'high'},options).ok,false);
  await assert.rejects(listModels({loadDisabledModels:()=>new Set(),loadAvailableModels:()=>c}),{code:'MODEL_POLICY_INVALID'});
  assert.equal((await recommendFromSnapshot('bounded',scheduling.DEFAULTS,snapshot,null,{},
    {mode:'auto',preferencesSaved:false})).code,'MODEL_POLICY_INVALID');
});

test('unreadable policy path and unknown local data directory fail closed',t=>{
  const file=policyFixture(t);
  fs.mkdirSync(file);
  assert.throws(()=>loadPolicy(),{code:'MODEL_POLICY_UNAVAILABLE'});
  assert.equal(auto(catalog()).code,'MODEL_POLICY_UNAVAILABLE');
  delete process.env.LOCALAPPDATA;
  assert.throws(()=>loadPolicy(),{code:'MODEL_POLICY_UNAVAILABLE'});
  assert.equal(auto(catalog()).code,'MODEL_POLICY_UNAVAILABLE');
});
