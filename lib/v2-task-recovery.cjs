'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { execFile } = require('node:child_process');
const { isAutoModel, lookupAutoSelection } = require('./auto-selection.cjs');
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const LIMIT = 2 * 1024 * 1024;
const MAX_TASK_BYTES = 8000;

function readSegment(file, tail = false) {
  let fd;
  try {
    fd=fs.openSync(file,'r');const size=fs.fstatSync(fd).size;
    const start=tail ? Math.max(0,size-LIMIT) : 0;
    const buf=Buffer.alloc(Math.min(size,LIMIT));fs.readSync(fd,buf,0,buf.length,start);
    const lines=buf.toString('utf8').split('\n');
    if (start>0) lines.shift();
    return lines.flatMap(line=>{try{return [JSON.parse(line)];}catch{return [];}});
  } catch { return []; }
  finally { if(fd!==undefined)fs.closeSync(fd); }
}

function childMetadata(input) {
  if (typeof input?.model !== 'string' || !input.model || input.model.trim() !== input.model) return {status:'unavailable'};
  if (!UUID.test(String(input.agent_id)) || !UUID.test(String(input.session_id)) || !input.transcript_path) return {status:'unavailable'};
  const childRecords=readSegment(input.transcript_path);
  const meta=childRecords.find(r=>r.type==='session_meta')?.payload;
  if(childRecords.some(r=>r.type==='turn_context' && r.payload?.model && r.payload.model!==input.model))return {status:'unavailable'};
  if (!meta || meta.id!==input.agent_id || meta.session_id!==input.session_id || meta.model && meta.model!==input.model) return {status:'unavailable'};
  if (meta.multi_agent_version==='v1') return {status:'not_needed'};
  if (meta.multi_agent_version!=='v2' || meta.model_provider!=='custom' || !UUID.test(String(meta.parent_thread_id))) return {status:'unavailable'};
  return {status:'v2',meta};
}

function recoverTask(input, parentPath, options = {}) {
  const child=childMetadata(input);
  if(child.status!=='v2')return {status:child.status};
  const first=readSegment(parentPath);
  const parent=first.find(r=>r.type==='session_meta')?.payload;
  if(!parent || parent.id!==child.meta.parent_thread_id) return {status:'unavailable'};
  const records=readSegment(parentPath,true);
  // Resolve the original call's parent turn, never the child's own turn.
  let turnId = null;
  for (const record of records) {
    const p = record.payload;
    if (record.type==='turn_context' && UUID.test(String(p?.turn_id))) turnId=p.turn_id;
    if (record.type==='event_msg' && p?.type==='task_started' && UUID.test(String(p.turn_id))) turnId=p.turn_id;
    const declared=p?.internal_chat_message_metadata_passthrough?.turn_id;
    record.parentTurnId=UUID.test(String(declared)) ? declared : turnId;
    record.turnConflict=UUID.test(String(declared)) && turnId && declared!==turnId;
  }
  const launches=records.filter(r=>r.type==='event_msg' && r.payload?.type==='item_completed'
    && r.payload?.item?.type==='SubAgentActivity' && r.payload.item.kind==='started'
    && r.payload.item.agent_thread_id===input.agent_id);
  // A path, model and nearby timestamp can belong to an earlier child with the
  // same task name. Wait for the actual child-to-call launch record instead.
  if(launches.length!==1)return {status:'unavailable'};
  const launch=launches[0];
  if(launch.payload.item.model!==input.model)return {status:'unavailable'};
  const callId=launch?.payload?.item?.id;
  if(typeof callId==='string' && callId.startsWith('exec-'))return {status:'unavailable'};
  const calls=typeof callId==='string' && callId ? records.filter(r=>r.type==='response_item' && r.payload?.type==='function_call'
    && r.payload.name==='spawn_agent' && r.payload.namespace==='collaboration' && r.payload.call_id===callId
    && (!launch.parentTurnId || r.parentTurnId===launch.parentTurnId)) : [];
  if(calls.length!==1)return {status:'unavailable'};
  const call=calls[0];
  if (call.turnConflict || launch.turnConflict || !UUID.test(String(call.parentTurnId))
      || launch.parentTurnId !== call.parentTurnId) return {status:'unavailable'};
  // Codex marks direct plaintext collaboration calls with an explicit empty
  // encrypted_function_args array. Missing is different: it is still routed
  // as an encrypted AgentMessage by the native tool handler.
  let args;try{args=JSON.parse(call.payload.arguments);}catch{return {status:'unavailable'};}
  if(!args || typeof args.message!=='string' || !args.message.trim()
      || Buffer.byteLength(args.message,'utf8')>MAX_TASK_BYTES)return {status:'unavailable'};
  const parentAgentPath=parent.agent_path || '/root';
  if(typeof args.task_name!=='string' || parentAgentPath.replace(/\/$/,'')+'/'+args.task_name!==child.meta.agent_path)return {status:'unavailable'};
  const automatic=isAutoModel(args.model,{allowOmitted:options.allowOmitted===true && !Object.hasOwn(args,'model')});
  if (!automatic) {
    if (args.model!==input.model) return {status:'unavailable'};
  } else {
    if (call.turnConflict || launch?.turnConflict || !UUID.test(String(call.parentTurnId))) return {status:'unavailable'};
    // Reject duplicate original IDs within the exact parent turn, even when
    // the startup fallback found only one matching task path.
    const peers=records.filter(r=>r.type==='response_item' && r.payload?.type==='function_call'
      && r.payload.namespace==='collaboration' && r.payload.name==='spawn_agent'
      && r.payload.call_id===call.payload.call_id && r.parentTurnId===call.parentTurnId);
    if (peers.length!==1) return {status:'unavailable'};
    const selected=lookupAutoSelection({parentThreadId:parent.id,turnId:call.parentTurnId,
      callId:call.payload.call_id,model:input.model},options);
    const callTime=Date.parse(call.timestamp), created=Date.parse(child.meta.timestamp);
    if (!selected || !Number.isFinite(callTime) || !Number.isFinite(created)
        || selected.createdAt < callTime || selected.createdAt > created) return {status:'unavailable'};
    const observed=[input.reasoning_effort,input.effort,launch?.payload.item.reasoning_effort,
      ...readSegment(input.transcript_path).filter(r=>r.type==='turn_context').map(r=>r.payload.effort ?? r.payload.reasoning_effort)];
    if (observed.some(e=>e!=null && e!==selected.effort)) return {status:'unavailable'};
  }
  if(Array.isArray(call.payload.encrypted_function_args) && call.payload.encrypted_function_args.length===0)return {status:'not_needed'};
  return {status:'restored',message:args.message};
}

function locateParent(parentId) {
  return new Promise(resolve=>{
    execFile('python',[path.join(__dirname,'..','scripts','parent-transcript.py'),parentId],
      {encoding:'utf8',timeout:1200,maxBuffer:4096,windowsHide:true,stdio:['ignore','pipe','pipe']},
      (error,stdout)=>{
        if(error)return resolve(null);
        try{resolve(JSON.parse(stdout).path || null);}catch{resolve(null);}
      });
  });
}

async function recoverStartupTask(input, locate = locateParent, options = {}) {
  const child=childMetadata(input);
  if(child.status!=='v2')return {status:child.status};
  const parentPath=await locate(child.meta.parent_thread_id);
  if(!parentPath)return {status:'unavailable'};
  const deadline=Date.now()+3000;
  let result=recoverTask(input,parentPath,options);
  for(let i=0;i<15 && result.status==='unavailable' && Date.now()<deadline;i++) {
    await new Promise(resolve=>setTimeout(resolve,Math.min(150,Math.max(0,deadline-Date.now()))));
    result=recoverTask(input,parentPath,options);
  }
  return result;
}

module.exports={recoverTask,recoverStartupTask,MAX_TASK_BYTES};
