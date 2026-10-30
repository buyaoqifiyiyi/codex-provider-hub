#!/usr/bin/env node
'use strict';

const path = require('node:path');
const { execFile } = require('node:child_process');
const SPAWN_NAMES = new Set(['spawn_agent', 'Agent', 'collaborationspawn_agent',
  'collaboration.spawn_agent', 'multi_agent_v1spawn_agent', 'multi_agent_v1__spawn_agent']);

function automaticRequest(model) {
  if (model === 'auto') return {task:'boundary',purpose:'tool'};
  if (typeof model !== 'string') return null;
  const match=/^auto:(bounded|boundary|critical):(text|tool|image)$/.exec(model);
  return match ? {task:match[1],purpose:match[2]} : null;
}

function automaticDenial(code, message) {
  return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'deny',
    permissionDecisionReason:`[subagent-model-guard/${code}] ${message}`}};
}

function readAutoSignals() {
  return new Promise(resolve=>{
    const executable=path.join(process.env.LOCALAPPDATA || '', 'CodexProviderHub','runtime','Scripts','python.exe');
    execFile(executable,[path.join(__dirname,'..','scripts','read-auto-signals.py')],
      {encoding:'utf8',timeout:5000,maxBuffer:1024*1024,windowsHide:true,stdio:['ignore','pipe','pipe']},
      (error,stdout)=>{
        if(error)return resolve({ok:false});
        try { const data=JSON.parse(stdout);resolve(data && data.ok===true ? data : {ok:false}); }
        catch { resolve({ok:false}); }
      });
  });
}

async function routeAutomatically(input, request, dependencies) {
  const deadline=Date.now()+26000;
  const local=require('../lib/disabled-models.cjs');
  const scheduler=require('../lib/scheduling.cjs');
  const guard=dependencies.guard || require('../lib/guard.cjs');
  const disabled=(dependencies.loadDisabledModels || local.loadDisabledModels)();
  const load=dependencies.loadAvailableModels || dependencies.options?.loadAvailableModels || require('../lib/model-catalog.cjs').loadAvailableModels;
  let available=local.filterDisabledModels(load(dependencies.options || {}),disabled);
  const required=input.tool_input?.reasoning_effort;
  if (required !== undefined && (typeof required!=='string' || !required || required.trim()!==required))
    return automaticDenial('AUTO_EFFORT_INVALID','指定的思考强度无效，未创建子代理。');
  if (required) available={...available,models:Object.fromEntries(Object.entries(available.models).map(([name,levels])=>
    [name,Array.isArray(levels) ? levels.filter(level=>level===required) : []]))};
  const signals=await (dependencies.readAutoSignals || readAutoSignals)();
  if (!signals || signals.ok!==true) return automaticDenial('AUTO_EVIDENCE_UNAVAILABLE','无法读取本机调度信息，未创建子代理。');
  const snapshot=scheduler.readRoutingSnapshot(),preferences=snapshot.preferences,mode=snapshot.mode;
  if (required && mode==='manual') {
    preferences[request.task]={...preferences[request.task],effort:required,fallback_effort:required};
  }
  const excluded=[];const rejectedModels=new Set();
  for (let attempt=0;attempt<3 && Date.now()<deadline;attempt++) {
    const chosen=scheduler.select(request.task,preferences,available,null,{mode,preferencesSaved:snapshot.saved,purpose:request.purpose,
      requiredEffort:required,evidence:signals.evidence,references:signals.references,excluded});
    if (!chosen.ok) return automaticDenial('AUTO_NO_ELIGIBLE_MODEL','没有符合任务和思考强度的可用模型，未创建子代理。');
    const effective={...input,tool_input:{...input.tool_input,model:chosen.model,reasoning_effort:chosen.effort}};
    let timer;
    const outcome=await Promise.race([
      runHook(effective,{...dependencies,_automaticResolved:true}),
      new Promise(resolve=>{timer=setTimeout(()=>resolve(automaticDenial('AUTO_PRECHECK_TIMEOUT','自动调度检查超时，未创建子代理。')),Math.max(1,deadline-Date.now()));})
    ]);
    clearTimeout(timer);
    if (outcome?.hookSpecificOutput?.permissionDecision === 'deny') {
      const message=outcome.hookSpecificOutput.permissionDecisionReason || '';
      if (/\/(?:MODEL_CATALOG_UNAVAILABLE|MODEL_GUARD_UNAVAILABLE|AUTO_PRECHECK_TIMEOUT)\]/.test(message)) return outcome;
      if (rejectedModels.has(chosen.model)) return outcome;
      rejectedModels.add(chosen.model);
      for (const effort of available.models[chosen.model] || []) excluded.push({model:chosen.model,effort});
      continue;
    }
    try {
      const recorder=dependencies.autoSelection || require('../lib/auto-selection.cjs');
      await recorder.recordAutoSelection(input,chosen,{...(dependencies.options || {}),parentThreadId:guard.parentThread(input)});
    } catch {
      return automaticDenial('AUTO_RECORD_UNAVAILABLE','无法确认自动调度记录，未创建子代理。');
    }
    return {hookSpecificOutput:{hookEventName:'PreToolUse',permissionDecision:'allow',updatedInput:effective.tool_input}};
  }
  return automaticDenial('AUTO_NO_READY_ROUTE','当前没有可确认的模型线路，未创建子代理。');
}

async function runHook(input, dependencies = {}) {
    const guard = dependencies.guard || require('../lib/guard.cjs');
    const isSpawn = input?.hook_event_name === 'PreToolUse' && SPAWN_NAMES.has(input?.tool_name);
    const automatic=isSpawn && !dependencies._automaticResolved && automaticRequest(input?.tool_input?.model);
    if (automatic) return routeAutomatically(input,automatic,dependencies);
    let options = { ...(dependencies.options || {}) };
    if (isSpawn) {
      const local = require('../lib/disabled-models.cjs');
      const disabled = (dependencies.loadDisabledModels || local.loadDisabledModels)();
      options.disabledModels = disabled;
      const load = dependencies.loadAvailableModels || options.loadAvailableModels || require('../lib/model-catalog.cjs').loadAvailableModels;
      options.loadAvailableModels = opts => (options.availableModels = load(opts));
      options.spawnValidation = guard.validateSpawn(input.tool_input, options);
      if (!options.spawnValidation.ok) return guard.handleHook(input, options);
    }
    const hub = isSpawn && require('../lib/model-policy.cjs').hubBinding(options.availableModels, input.tool_input?.model);
    if (hub && options.spawnValidation?.ok) {
      const reject = code => {
        const reasons = {
          HUB_BINDING_UNAVAILABLE:'模型连接已停用、绑定失效或与当前目录不一致，已拒绝创建；请检查当前启用的绑定。',
          HUB_BINDING_CHANGED:'模型连接绑定在校验期间发生变化，已拒绝创建；请先核对绑定及已有任务，勿自动重复创建。',
          HUB_PARENT_ROUTE_UNKNOWN:'未能确认父会话同轮成功出口与该模型的当前绑定及引擎配置一致，已拒绝创建。',
          HUB_TASK_DELIVERY_UNSUPPORTED:'无法确认父会话使用 V1 或 V2 派发协议，不能保证任务正文交付，已拒绝创建。',
          HUB_V2_TASK_INVALID:'V2 任务正文缺失、为空或超过 8000 UTF-8 字节，无法确认完整交付；请精简或拆分后重试。'
        };
        options.spawnValidation = {ok:false,code,message:reasons[code] || '模型连接或任务交付无法安全核验，已拒绝创建。',model:input.tool_input.model,effort:input.tool_input.reasoning_effort};
        return guard.handleHook(input, options);
      };
      const read = dependencies.resolveBindingKind || require('../lib/binding-kind.cjs').resolveBindingKind;
      let before, after, parent;
      try { before = await read(input.tool_input.model); } catch { return reject('HUB_BINDING_UNAVAILABLE'); }
      const matches = b => b && b.kind === 'bound' && b.revision === hub.revision
        && b.model === hub.model && b.connection === hub.connection;
      if (!matches(before)) return reject('HUB_BINDING_UNAVAILABLE');
      const context = guard.senderContext(input);
      if (!['v1','v2'].includes(context?.version)) return reject('HUB_TASK_DELIVERY_UNSUPPORTED');
      if (context.version === 'v2' && (typeof input.tool_input.message !== 'string' || !input.tool_input.message.trim()
          || Buffer.byteLength(input.tool_input.message,'utf8') > require('../lib/v2-task-recovery.cjs').MAX_TASK_BYTES)) return reject('HUB_V2_TASK_INVALID');
      try {
        parent = await (dependencies.resolveParentRoute || require('../lib/parent-route.cjs').resolveParentRoute)(guard.parentProvider(input),
          {...input, routeModel:input.tool_input.model, routeRevision:hub.revision});
      } catch { parent = 'unknown'; }
      try { after = await read(input.tool_input.model); } catch { return reject('HUB_BINDING_CHANGED'); }
      if (!matches(after) || after.kind !== before.kind) return reject('HUB_BINDING_CHANGED');
      if (parent !== 'compatible') return reject('HUB_PARENT_ROUTE_UNKNOWN');
      options.hubVerified = true;
    }
    if (input?.hook_event_name === 'SubagentStart') {
      try {
        const catalog = (dependencies.loadAvailableModels || require('../lib/model-catalog.cjs').loadAvailableModels)(options);
        const local = require('../lib/disabled-models.cjs');
        const disabled = (dependencies.loadDisabledModels || local.loadDisabledModels)();
        const bound = require('../lib/model-policy.cjs').hubBinding(local.filterDisabledModels(catalog,disabled),input.model);
        const official = !disabled.has(input.model) && Object.hasOwn(catalog.models || {},input.model)
          && [catalog.nativeModels,catalog.managedOfficialModels].some(models=>Array.isArray(models) && models.includes(input.model))
          && !Object.hasOwn(catalog.hubModels || {},input.model);
        if (bound) options.taskRecovery = await (dependencies.recoverStartupTask || require('../lib/v2-task-recovery.cjs').recoverStartupTask)(input,undefined,options);
        else if (!official) options.taskRecovery = {status:'unavailable'};
      } catch { options.taskRecovery = {status:'unavailable'}; }
    }
    if (input?.hook_event_name === 'PreToolUse' && guard.isV2MessageTool(input?.tool_name)) {
      const context=guard.senderContext(input);
      options.agentVersion=context?.version;
      if(context?.version==='v2') options.targetModel=await (dependencies.messageTargetModel || require('../lib/parent-route.cjs').messageTargetModel)(context,input.tool_input?.target);
      if(context?.version==='v2' && options.targetModel) {
        const catalog=(dependencies.loadAvailableModels || require('../lib/model-catalog.cjs').loadAvailableModels)(options);
        options.targetHub=!!require('../lib/model-policy.cjs').hubBinding(catalog,options.targetModel);
        const disabled=(dependencies.loadDisabledModels || require('../lib/disabled-models.cjs').loadDisabledModels)();
        options.targetUnavailable=disabled.has(options.targetModel) || !Object.hasOwn(catalog.models || {},options.targetModel);
        options.targetHub ||= Object.hasOwn(catalog.hubModels || {},options.targetModel);
        options.targetOfficial=[catalog.nativeModels,catalog.managedOfficialModels]
          .some(models=>Array.isArray(models) && models.includes(options.targetModel));
      }
    }
    return guard.handleHook(input, options);
}

function startStdin() {
  let body = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', chunk => { body += chunk; });
  process.stdin.on('end', async () => {
    let input;
    try {
      input = JSON.parse(body);
      const output = await runHook(input);
      if (output) process.stdout.write(JSON.stringify(output) + '\n');
    } catch (error) {
      if (input?.hook_event_name === 'PreToolUse') {
        process.stdout.write(JSON.stringify({ hookSpecificOutput: { hookEventName: 'PreToolUse',
          permissionDecision: 'deny', permissionDecisionReason: error.code === 'MODEL_DISABLE_STATE_UNAVAILABLE'
            ? '[subagent-model-guard/MODEL_DISABLE_STATE_UNAVAILABLE] 本机连接停用状态不可读取，子代理创建已拒绝。'
            : '[subagent-model-guard/MODEL_GUARD_UNAVAILABLE] 校验程序不可用，子代理创建已拒绝。' } }) + '\n');
        return;
      }
      process.stderr.write('subagent-model-guard: malformed hook envelope; host coverage unverified.\n');
    }
  });
}

if (require.main === module) startStdin();

module.exports = { runHook, startStdin, automaticRequest };
