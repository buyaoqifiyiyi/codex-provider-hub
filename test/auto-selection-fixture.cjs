'use strict';
const fs=require('node:fs'),path=require('node:path');
const {makeTestDir}=require('./safe-temp-fixture.cjs');
const {recordAutoSelection}=require('../lib/auto-selection.cjs');
const P='10000000-0000-4000-8000-000000000001', C='10000000-0000-4000-8000-000000000002';
const S='10000000-0000-4000-8000-000000000003', T='10000000-0000-4000-8000-000000000004';
const MODEL='hub-fixture/synthetic-model';
function fixture(t,marker='auto') {
 const root=makeTestDir(t,'hub-auto-selection-');
 const parent=path.join(root,'parent.jsonl'),child=path.join(root,'child.jsonl'),now=Date.now();
 const args={model:marker,task_name:'task',message:'synthetic full task'};if(marker===undefined)delete args.model;
 const meta={type:'session_meta',payload:{id:C,session_id:S,parent_thread_id:P,multi_agent_version:'v2',model_provider:'custom',agent_path:'/root/task',timestamp:new Date(now-100).toISOString()}};
 const call={type:'response_item',timestamp:new Date(now-300).toISOString(),payload:{type:'function_call',namespace:'collaboration',name:'spawn_agent',call_id:'call-1',arguments:JSON.stringify(args)}};
 const launch={type:'event_msg',payload:{type:'item_completed',item:{type:'SubAgentActivity',kind:'started',agent_thread_id:C,model:MODEL,reasoning_effort:'high',id:'call-1'}}};
 const parentMeta={type:'session_meta',payload:{id:P,agent_path:'/root'}};
 const turn={type:'turn_context',payload:{turn_id:T}};
 const e={hook_event_name:'SubagentStart',model:MODEL,agent_id:C,session_id:S,transcript_path:child};
 const options={dataDir:root,now,allowOmitted:true};
 const write=(rows=[parentMeta,turn,call,launch],children=[meta])=>{fs.writeFileSync(parent,rows.map(JSON.stringify).join('\n'));fs.writeFileSync(child,children.map(JSON.stringify).join('\n'));};
 const record=(overrides={})=>recordAutoSelection({session_id:P,turn_id:T,tool_use_id:'call-1',tool_input:args},{model:MODEL,reasoning_effort:'high'},{...options,now:now-200,...overrides});
 const query={parentThreadId:P,turnId:T,callId:'call-1',model:MODEL};
 write();return {root,parent,child,now,args,meta,call,launch,parentMeta,turn,e,options,write,record,query};
}
module.exports={fixture,P,C,S,T,MODEL};
