'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {spawn}=require('node:child_process');
const {setTimeout:delay}=require('node:timers/promises');
const scheduling=require('../lib/scheduling.cjs');
const selection=require('../lib/auto-selection.cjs');
const python=process.env.PROVIDER_HUB_PYTHON;
const script=path.resolve(__dirname,'../scripts/schedule-state.py');
const P='44444444-4444-4444-8444-444444444444',T='55555555-5555-4555-8555-555555555555';
const input={session_id:P,turn_id:T,tool_use_id:'race',tool_input:{model:'auto'}};
const prefs=model=>Object.fromEntries(scheduling.TASKS.map(task=>[task,{model,effort:'high',fallback_model:'native-fixture-fallback',fallback_effort:'high'}]));
const first=prefs('native-fixture-a'),second=prefs('native-fixture-b');
function fixture(t){
 const root=makeTestDir(t,'locks-integration-');
 const old=process.env.LOCALAPPDATA;process.env.LOCALAPPDATA=root;
 t.after(()=>{if(old===undefined)delete process.env.LOCALAPPDATA;else process.env.LOCALAPPDATA=old;});
 return root;
}
function processResult(command,args,env){
 const child=spawn(command,args,{env,windowsHide:true,stdio:['ignore','pipe','pipe']});
 let stdout='',stderr='',done=false;
 const completion=new Promise((resolve,reject)=>{
  child.stdout.on('data',b=>stdout+=b);child.stderr.on('data',b=>stderr+=b);
  child.on('error',reject);child.on('close',(code,signal)=>{done=true;resolve({code,signal,stdout,stderr});});
 });
 return {child,completion,get done(){return done;}};
}
async function until(predicate){const end=Date.now()+10000;while(!predicate()){if(Date.now()>end)throw Error('barrier timeout');await delay(10);}}
function worker(t,root,request){
 const barrier=fs.mkdtempSync(path.join(root,'barrier-'));
 const requestFile=path.join(barrier,'request.json');fs.writeFileSync(requestFile,JSON.stringify(request));
 const proc=processResult(python,['-I','-B',path.join(__dirname,'transaction-worker.py'),script,requestFile,barrier],process.env);
 t.after(async()=>{if(!proc.done)proc.child.kill('SIGKILL');await proc.completion;});
 return {...proc,barrier,ready:()=>fs.existsSync(path.join(barrier,'ready')),release:()=>fs.writeFileSync(path.join(barrier,'release'),'')};
}
function api(root,body){
 const prelude=`process.env.LOCALAPPDATA=${JSON.stringify(root)};const scheduling=require(${JSON.stringify(path.resolve(__dirname,'../lib/scheduling.cjs'))});const selection=require(${JSON.stringify(path.resolve(__dirname,'../lib/auto-selection.cjs'))});`;
 const marker=path.join(root,'contender-'+require('node:crypto').randomBytes(6).toString('hex'));
 const proc=processResult(process.execPath,['-e',prelude+`require('node:fs').writeFileSync(${JSON.stringify(marker)},'entered');`+body],{...process.env,LOCALAPPDATA:root});
 return Object.assign(proc,{entered:()=>fs.existsSync(marker)});
}
for(const action of ['mode','manual'])test('read/merge/commit serialized: holder '+action,async t=>{
 const root=fixture(t);scheduling.saveManualRouting(first);
 const request={root:path.join(root,'CodexProviderHub'),action,...(action==='mode'?{mode:'auto'}:{preferences:second})};
 const holder=worker(t,root,request);await until(holder.ready);
 const other=api(root,action==='mode'?`console.log(JSON.stringify(scheduling.saveManualRouting(${JSON.stringify(second)})));`:`console.log(JSON.stringify(scheduling.saveRoutingMode('auto')));`);
 t.after(async()=>{if(!other.done)other.child.kill('SIGKILL');await other.completion;});
 // Wait until the contender's Python helper is running by using its Node event
 // loop-independent launch marker before the public synchronous API call.
 await until(other.entered);await delay(200);assert.equal(other.done,false,'contender bypassed the transaction');
 holder.release();assert.equal((await holder.completion).code,0);
 const completed=await other.completion;assert.equal(completed.code,0,completed.stderr);
 assert.deepEqual(scheduling.readPreferences(),second);
 assert.equal(scheduling.readRoutingMode(),action==='mode'?'manual':'auto');
});
test('snapshot waits for an in-flight mode and preference transaction',async t=>{
 const root=fixture(t);scheduling.savePreferences(first);scheduling.saveRoutingMode('manual');
 const request={root:path.join(root,'CodexProviderHub'),action:'manual',preferences:second};
 const holder=worker(t,root,request);await until(holder.ready);
 const reader=api(root,`console.log(JSON.stringify(scheduling.readRoutingSnapshot()));`);
 t.after(async()=>{if(!reader.done)reader.child.kill('SIGKILL');await reader.completion;});
 await until(reader.entered);await delay(200);assert.equal(reader.done,false,'snapshot bypassed the transaction');
 holder.release();assert.equal((await holder.completion).code,0);
 const result=await reader.completion;assert.equal(result.code,0,result.stderr);
 const snapshot=JSON.parse(result.stdout);assert.equal(snapshot.mode,'manual');assert.deepEqual(snapshot.preferences,second);
});
for(const action of ['mode','preferences'])test('preference save preserves the latest mode under concurrent writes: '+action,async t=>{
 const root=fixture(t);scheduling.saveRoutingPreferences(first);
 const request={root:path.join(root,'CodexProviderHub'),action,...(action==='mode'?{mode:'manual'}:{preferences:second})};
 const holder=worker(t,root,request);await until(holder.ready);
 const other=api(root,action==='mode'?`console.log(JSON.stringify(scheduling.saveRoutingPreferences(${JSON.stringify(second)})));`
   :`console.log(JSON.stringify(scheduling.saveRoutingMode('manual')));`);
 t.after(async()=>{if(!other.done)other.child.kill('SIGKILL');await other.completion;});
 await until(other.entered);await delay(200);assert.equal(other.done,false,'contender bypassed the transaction');
 holder.release();assert.equal((await holder.completion).code,0);
 const completed=await other.completion;assert.equal(completed.code,0,completed.stderr);
 const snapshot=scheduling.readRoutingSnapshot();assert.equal(snapshot.mode,'manual');assert.deepEqual(snapshot.preferences,second);
});
for(const action of ['mode','manual','record'])test('force-killed transaction releases lock: '+action,async t=>{
 const root=fixture(t);scheduling.saveManualRouting(first);
 const record={parentThreadId:P,turnId:T,callId:'race',model:'native-fixture-b',effort:'high'};
 const request=action==='record'?{root,action,record,now:1000}:{root:path.join(root,'CodexProviderHub'),action,...(action==='mode'?{mode:'auto'}:{preferences:second})};
 const holder=worker(t,root,request);await until(holder.ready);
 assert(holder.child.kill('SIGKILL'));await holder.completion;
 if(action==='record'){
  const value=selection.recordAutoSelection(input,{model:'native-fixture-a',effort:'high'},{dataDir:root,now:1000});
  assert.equal(value.model,'native-fixture-a');
  assert.equal(fs.readdirSync(path.join(root,'auto-selections')).length,1,'pending file not reclaimed');
 }else{
  assert.deepEqual(scheduling.readPreferences(),first,'uncommitted snapshot became visible');
  scheduling.saveManualRouting(second);scheduling.saveRoutingMode('auto');
  assert.deepEqual(scheduling.readPreferences(),second);assert.equal(scheduling.readRoutingMode(),'auto');
 }
});
test('parallel public API record writers keep one exact immutable identity',async t=>{
 const root=fixture(t);
 const writers=Array.from({length:6},(_,i)=>api(root,`try{selection.recordAutoSelection(${JSON.stringify(input)},{model:${JSON.stringify(i%2?'native-fixture-b':'native-fixture-a')},effort:'high'},{dataDir:${JSON.stringify(root)},now:1000});console.log('ok')}catch(e){console.log(e.code)}`));
 const results=await Promise.all(writers.map(p=>p.completion));
 assert(results.every(r=>r.code===0 && ['ok','AUTO_SELECTION_CONFLICT'].includes(r.stdout.trim())),JSON.stringify(results));
 const dir=path.join(root,'auto-selections');const files=fs.readdirSync(dir);assert.equal(files.length,1);
 const r=JSON.parse(fs.readFileSync(path.join(dir,files[0])));assert.equal(r.callId,'race');
 assert(selection.lookupAutoSelection({parentThreadId:P,turnId:T,callId:'race',model:r.model},{dataDir:root,now:1001}));
 assert.equal(selection.lookupAutoSelection({parentThreadId:P,turnId:T,callId:'race',model:'wrong'},{dataDir:root,now:1001}),null);
 const db=fs.readFileSync(path.join(root,'auto-selections-lock.sqlite3'));assert(!db.includes(Buffer.from(P)));
});
