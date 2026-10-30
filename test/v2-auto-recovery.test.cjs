'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {fixture,P,C,T,MODEL}=require('./auto-selection-fixture.cjs');
const {recordAutoSelection,TTL_MS}=require('../lib/auto-selection.cjs');
const {recoverTask,recoverStartupTask}=require('../lib/v2-task-recovery.cjs');
const run=f=>recoverTask(f.e,f.parent,f.options);
// Ported recovery-only cases from source test/guard-dynamic.test.cjs:69-104.
for(const model of [MODEL,'legacy-synthetic/model-a'])test('legacy explicit exact model, startup and already-plaintext: '+model,async t=>{
 const f=fixture(t,model);f.e.model=model;f.launch.payload.item.model=model;f.write();
 assert.deepEqual(run(f),{status:'restored',message:'synthetic full task'});
 assert.deepEqual(await recoverStartupTask(f.e,async()=>f.parent,f.options),{status:'restored',message:'synthetic full task'});
 f.call.payload.encrypted_function_args=[];f.write();assert.equal(run(f).status,'not_needed');
});
for(const change of ['model','parent','path','callId','duplicateCall','duplicateLaunch','oversize','childModel'])test('legacy strict rejection: '+change,t=>{
 const f=fixture(t,MODEL);let rows;
 if(change==='model')f.call.payload.arguments=JSON.stringify({...f.args,model:'wrong'});
 if(change==='parent')f.parentMeta.payload.id=T;
 if(change==='path')f.meta.payload.agent_path='/root/other';
 if(change==='callId')f.launch.payload.item.id='different';
 if(change==='duplicateCall')rows=[f.parentMeta,f.turn,f.call,f.call,f.launch];
 if(change==='duplicateLaunch')rows=[f.parentMeta,f.turn,f.call,f.launch,f.launch];
 if(change==='oversize')f.call.payload.arguments=JSON.stringify({...f.args,message:'x'.repeat(8001)});
 if(change==='childModel')f.meta.payload.model='wrong';
 f.write(rows);assert.equal(run(f).status,'unavailable');
});
test('legacy pre-launch waits for the actual child identity',t=>{
 const f=fixture(t,MODEL);f.write([f.parentMeta,f.turn,f.call]);assert.equal(run(f).status,'unavailable');
 f.write([f.parentMeta,f.turn,f.call,f.call]);assert.equal(run(f).status,'unavailable');
});
test('legacy pre-launch never guesses between different explicitly selected models',t=>{
 const f=fixture(t,MODEL);const other=structuredClone(f.call);
 other.payload.call_id='call-other';other.payload.arguments=JSON.stringify({...f.args,model:'other-model'});
 f.write([f.parentMeta,f.turn,f.call,other]);
 assert.equal(run(f).status,'unavailable');
});
for(const marker of ['auto','auto:bounded:tool','auto:boundary:image','auto:critical:text','omitted'])test('automatic original call recovers exact body after launch identity: '+marker,t=>{
 const f=fixture(t,marker);
 if(marker==='omitted'){delete f.args.model;f.call.payload.arguments=JSON.stringify(f.args);}
 f.record();f.write();assert.deepEqual(run(f),{status:'restored',message:'synthetic full task'});
 f.write([f.parentMeta,f.turn,f.call]);assert.equal(run(f).status,'unavailable');
 f.call.payload.encrypted_function_args=[];f.write();assert.equal(run(f).status,'not_needed');
});
for(const kind of ['missing','expired','wrongTurn','wrongParent','wrongCall','wrongModel','noTurn','declaredTurnConflict','wrongEffort','childEffort','writtenAfterChild','writtenBeforeCall','oversize','multibyteOversize','emptyMessage','wrongPath','duplicateCall','duplicateLaunch','duplicateSameIdOtherPath','emptyModel','null','unknown','omissionNotAllowed'])test('automatic fail closed: '+kind,t=>{
 const f=fixture(t);let rows;
 if(kind!=='missing')f.record();
 if(kind==='expired')f.options.now=f.now+TTL_MS;
 if(kind==='wrongTurn')f.turn.payload.turn_id=P;
 if(kind==='wrongParent')f.meta.payload.parent_thread_id=T;
 if(kind==='wrongCall')f.call.payload.call_id='different';
 if(kind==='wrongModel'){f.e.model='other';f.launch.payload.item.model='other';}
 if(kind==='noTurn')rows=[f.parentMeta,f.call,f.launch];
 if(kind==='declaredTurnConflict')f.call.payload.internal_chat_message_metadata_passthrough={turn_id:P};
 if(kind==='wrongEffort')f.launch.payload.item.reasoning_effort='medium';
 if(kind==='childEffort')f.e.reasoning_effort='medium';
 if(kind==='writtenAfterChild')f.meta.payload.timestamp=new Date(f.now-201).toISOString();
 if(kind==='writtenBeforeCall')f.call.timestamp=new Date(f.now-199).toISOString();
 if(kind==='oversize')f.args.message='x'.repeat(8001);
 if(kind==='multibyteOversize')f.args.message='界'.repeat(2667);
 if(kind==='emptyMessage')f.args.message=' ';
 if(kind==='wrongPath')f.meta.payload.agent_path='/root/other';
 if(kind==='duplicateCall')rows=[f.parentMeta,f.turn,f.call,f.call,f.launch];
 if(kind==='duplicateLaunch')rows=[f.parentMeta,f.turn,f.call,f.launch,f.launch];
 if(kind==='duplicateSameIdOtherPath'){
  const other=structuredClone(f.call);other.payload.arguments=JSON.stringify({...f.args,task_name:'other'});
  rows=[f.parentMeta,f.turn,f.call,other];
 }
 if(kind==='emptyModel')f.args.model='';
 if(kind==='null')f.args.model=null;
 if(kind==='unknown')f.args.model='auto:unknown:text';
 if(kind==='omissionNotAllowed'){delete f.args.model;f.options.allowOmitted=false;}
 f.call.payload.arguments=JSON.stringify(f.args);f.write(rows);assert.equal(run(f).status,'unavailable');
});
test('same-model concurrent tasks recover by actual child and call ID',t=>{
 const f=fixture(t);f.record();
 const other=structuredClone(f.call);other.payload.call_id='call-2';other.payload.arguments=JSON.stringify({...f.args,task_name:'other',message:'SECOND exact body'});
 recordAutoSelection({session_id:P,turn_id:T,tool_use_id:'call-2',tool_input:f.args},{model:MODEL,effort:'high'},{...f.options,now:f.now-200});
 f.write([f.parentMeta,f.turn,f.call,other,f.launch]);assert.deepEqual(run(f),{status:'restored',message:'synthetic full task'});
 f.meta.payload.agent_path='/root/other';f.meta.payload.id=T;f.e.agent_id=T;
 const secondLaunch=structuredClone(f.launch);secondLaunch.payload.item.agent_thread_id=T;secondLaunch.payload.item.id='call-2';
 f.write([f.parentMeta,f.turn,f.call,other,f.launch,secondLaunch]);assert.deepEqual(run(f),{status:'restored',message:'SECOND exact body'});
 other.payload.arguments=JSON.stringify({...f.args,message:'different body, SAME path'});
 f.meta.payload.agent_path='/root/task';f.write([f.parentMeta,f.turn,f.call,other]);assert.equal(run(f).status,'unavailable');
});
test('same call_id across parent turns uses launch turn; stale turn cannot authorize current',t=>{
 const f=fixture(t);f.record();const turn2={type:'turn_context',payload:{turn_id:P}};
 const next=structuredClone(f.call);next.payload.arguments=JSON.stringify({...f.args,message:'SECOND TURN'});
 f.write([f.parentMeta,f.turn,f.call,turn2,next,f.launch]);assert.equal(run(f).status,'unavailable');
 recordAutoSelection({session_id:P,turn_id:P,tool_use_id:'call-1',tool_input:f.args},{model:MODEL,effort:'high'},{...f.options,now:f.now-200});
 assert.deepEqual(run(f),{status:'restored',message:'SECOND TURN'});
 f.write([f.parentMeta,f.turn,f.call,turn2,next]);assert.equal(run(f).status,'unavailable');
});
test('selection audit cannot override a real explicit model',t=>{
 const f=fixture(t);f.record();f.args.model='explicit-other';f.call.payload.arguments=JSON.stringify(f.args);f.write();
 assert.equal(run(f).status,'unavailable');
});
test('8000 UTF-8 bytes preserved exactly; V1 does not need recovery',t=>{
 const f=fixture(t);f.record();f.args.message='界'.repeat(2666)+'ab';f.call.payload.arguments=JSON.stringify(f.args);f.write();
 assert.equal(Buffer.byteLength(f.args.message),8000);assert.equal(run(f).message,f.args.message);
 f.meta.payload.multi_agent_version='v1';f.write();assert.equal(run(f).status,'not_needed');
});
test('tampered selection never recovers',t=>{
 const f=fixture(t);f.record();const dir=path.join(f.root,'auto-selections'),file=path.join(dir,fs.readdirSync(dir)[0]);
 const r=JSON.parse(fs.readFileSync(file));r.effort='medium';fs.writeFileSync(file,JSON.stringify(r));assert.equal(run(f).status,'unavailable');
});
test('startup bounded retry waits for original call, then uses prewritten audit',async t=>{
 const f=fixture(t);f.record();f.write([f.parentMeta,f.turn]);
 const timer=setTimeout(()=>f.write([f.parentMeta,f.turn,f.call,f.launch]),80);t.after(()=>clearTimeout(timer));
 assert.deepEqual(await recoverStartupTask(f.e,async()=>f.parent,f.options),{status:'restored',message:'synthetic full task'});
});
test('a prior child using the same path cannot supply its task to a new child',t=>{
 const f=fixture(t);f.record();
 f.launch.payload.item.agent_thread_id='10000000-0000-4000-8000-000000000005';
 f.write([f.parentMeta,f.turn,f.call,f.launch]);
 assert.equal(run(f).status,'unavailable');
});
test('startup retry cannot invent missing call, record, or body',async t=>{
 const f=fixture(t);f.write([f.parentMeta,f.turn]);assert.equal((await recoverStartupTask(f.e,async()=>f.parent,f.options)).status,'unavailable');
});
