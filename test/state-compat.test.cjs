'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {createHash}=require('node:crypto');
const {recordAutoSelection,lookupAutoSelection}=require('../lib/auto-selection.cjs');
const P='44444444-4444-4444-8444-444444444444',T='55555555-5555-4555-8555-555555555555';
for(const now of [1,Number.MAX_SAFE_INTEGER])test('existing JSON/checksum compatible at clock '+now,t=>{
 const root=makeTestDir(t,'lock-compat-');
 const fields=['schemaVersion','parentThreadId','turnId','callId','model','effort','createdAt','expiresAt'];
 const r={schemaVersion:1,parentThreadId:P,turnId:T,callId:'compat',model:'native-fixture',effort:'high',createdAt:now,expiresAt:now+300000};
 r.checksum=createHash('sha256').update(JSON.stringify(fields.map(f=>r[f]))).digest('hex');
 const dir=path.join(root,'auto-selections');fs.mkdirSync(dir);
 const name=createHash('sha256').update(JSON.stringify([P,T,'compat'])).digest('hex')+'.json';
 fs.writeFileSync(path.join(dir,name),JSON.stringify(r).replace('"schemaVersion":1,','"schemaVersion":1.0,'));
 assert.deepEqual(lookupAutoSelection({...r},{dataDir:root,now}),r);
 const row=recordAutoSelection({session_id:P,turn_id:T,tool_use_id:'compat',tool_input:{model:'auto'}},{model:r.model,effort:r.effort},{dataDir:root,now});
 assert.deepEqual(row,r);
});
