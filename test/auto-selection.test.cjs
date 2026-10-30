'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {spawnSync,spawn}=require('node:child_process');
const {fixture,P,T,MODEL}=require('./auto-selection-fixture.cjs');
const {recordAutoSelection:record,lookupAutoSelection:lookup,isAutoModel,TTL_MS,MAX_RECORDS}=require('../lib/auto-selection.cjs');
test('auto markers are exact; omission requires explicit option',()=>{
 for(const v of ['auto',...['bounded','boundary','critical'].flatMap(t=>['text','tool','image'].map(p=>`auto:${t}:${p}`))])assert.equal(isAutoModel(v),true);
 for(const v of ['',null,undefined,'Auto',' auto','auto:critical:audio','auto:other:tool','auto:critical:text:extra','native-fixture',{}])assert.equal(isAutoModel(v),false);
 assert.equal(isAutoModel(undefined,{allowOmitted:true}),true);assert.equal(isAutoModel(null,{allowOmitted:true}),false);
});
test('cross-process read; private payload excludes input and secret fields',t=>{
 const f=fixture(t);const row=f.record();
 const files=fs.readdirSync(path.join(f.root,'auto-selections'));assert.equal(files.length,1);
 const body=fs.readFileSync(path.join(f.root,'auto-selections',files[0]),'utf8');assert(!body.includes('synthetic full task'));assert(!body.includes(f.root));
 assert.deepEqual(Object.keys(row).sort(),['schemaVersion','parentThreadId','turnId','callId','model','effort','createdAt','expiresAt','checksum'].sort());
 const code=`console.log(JSON.stringify(require(${JSON.stringify(path.resolve(__dirname,'../lib/auto-selection.cjs'))}).lookupAutoSelection(${JSON.stringify(f.query)},${JSON.stringify(f.options)})))`;
 const child=spawnSync(process.execPath,['-e',code],{encoding:'utf8',env:{SystemRoot:process.env.SystemRoot,PATH:process.env.PATH}});
 assert.equal(child.status,0,child.stderr);assert.equal(JSON.parse(child.stdout).effort,'high');
 assert.equal(lookup({...f.query,turnId:P},f.options),null);assert.equal(lookup({...f.query,model:'wrong'},f.options),null);
});
test('same key is idempotent but conflicting model or effort rejected',t=>{
 const f=fixture(t);const a=f.record();assert.deepEqual(f.record(),a);
 for(const resolved of [{model:MODEL,effort:'medium'},{model:'other',effort:'high'}])assert.throws(()=>record({session_id:P,turn_id:T,tool_use_id:'call-1',tool_input:f.args},resolved,f.options),{code:'AUTO_SELECTION_CONFLICT'});
 assert.equal(lookup(f.query,f.options).effort,'high');
});
test('expiry and future records never match',t=>{
 const f=fixture(t);const r=f.record();assert.equal(lookup(f.query,{...f.options,now:r.expiresAt}),null);
 assert.equal(lookup(f.query,{...f.options,now:r.createdAt-1}),null);
 assert.throws(()=>f.record({now:r.expiresAt}),{code:'AUTO_SELECTION_EXPIRED'});
});
for(const mutation of ['model','effort','parent','extra','truncated','oversize','renamed'])test('tampered record rejected: '+mutation,t=>{
 const f=fixture(t);f.record();const dir=path.join(f.root,'auto-selections'),file=path.join(dir,fs.readdirSync(dir)[0]);let r=JSON.parse(fs.readFileSync(file));
 if(mutation==='model')r.model='changed';if(mutation==='effort')r.effort='medium';if(mutation==='parent')r.parentThreadId=T;if(mutation==='extra')r.message='forbidden';
 if(mutation==='renamed')fs.renameSync(file,path.join(dir,'0'.repeat(64)+'.json'));
 else fs.writeFileSync(file,mutation==='truncated'?'{' : mutation==='oversize'?'x'.repeat(1025):JSON.stringify(r));
 assert.equal(lookup(f.query,f.options),null);
});
test('explicit models, null, empty and present undefined are never auto records',t=>{
 const f=fixture(t);
 for(const v of [MODEL,null,'',undefined])assert.throws(()=>record({session_id:P,turn_id:T,tool_use_id:'call-1',tool_input:{model:v}},{model:MODEL,effort:'high'},f.options),{code:'AUTO_SELECTION_NOT_AUTO'});
});
test('missing model only allowed explicitly; parent override supports nested parents',t=>{
 const f=fixture(t);const input={session_id:P,turn_id:T,tool_use_id:'call-1',tool_input:{}};
 assert.throws(()=>record(input,{model:MODEL,effort:'high'},{dataDir:f.root}),{code:'AUTO_SELECTION_NOT_AUTO'});
 record(input,{model:MODEL,effort:'high'},{...f.options,parentThreadId:T});
 assert(lookup({...f.query,parentThreadId:T},f.options));
});
test('capacity is bounded, expired records reclaimed, corrupt stores fail closed',t=>{
 const f=fixture(t);
 for(let i=0;i<MAX_RECORDS;i++)record({session_id:P,turn_id:T,tool_use_id:'call-'+i,tool_input:f.args},{model:MODEL,effort:'high'},f.options);
 const next={session_id:P,turn_id:T,tool_use_id:'next',tool_input:f.args};
 assert.throws(()=>record(next,{model:MODEL,effort:'high'},f.options),{code:'AUTO_SELECTION_CAPACITY'});
 record(next,{model:MODEL,effort:'high'},{...f.options,now:f.now+TTL_MS});
 assert.equal(fs.readdirSync(path.join(f.root,'auto-selections')).length,1);
});
test('an inert legacy lock does not block the new process-owned lock',t=>{
 const f=fixture(t);f.record();const lock=path.join(f.root,'auto-selections/.lock');fs.mkdirSync(lock);
 assert.equal(lookup(f.query,f.options).effort,'high');assert.equal(f.record().effort,'high');assert(fs.existsSync(lock));
});
test('two OS processes with same key and conflicting choices cannot overwrite',async t=>{
 const f=fixture(t);const module=path.resolve(__dirname,'../lib/auto-selection.cjs');
 const start=model=>new Promise((resolve,reject)=>{
  const input={session_id:P,turn_id:T,tool_use_id:'race',tool_input:f.args};
  const code=`const m=require(${JSON.stringify(module)});try{m.recordAutoSelection(${JSON.stringify(input)},{model:${JSON.stringify(model)},effort:'high'},${JSON.stringify(f.options)});console.log('ok')}catch(e){console.log(e.code)}`;
  const p=spawn(process.execPath,['-e',code],{env:{SystemRoot:process.env.SystemRoot,PATH:process.env.PATH}});let text='';p.stdout.on('data',x=>text+=x);p.on('error',reject);p.on('exit',code=>code===0?resolve(text.trim()):reject(new Error('writer failed')));
 });
 const values=await Promise.all([start(MODEL),start('other')]);assert.equal(values.filter(v=>v==='ok').length,1);
 assert(values.every(v=>['ok','AUTO_SELECTION_CONFLICT','AUTO_SELECTION_BUSY'].includes(v)));
 assert.equal(fs.readdirSync(path.join(f.root,'auto-selections')).length,1);
});
