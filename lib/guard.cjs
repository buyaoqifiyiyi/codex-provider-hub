'use strict';

const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { validateSpawn: validateSelection, loadPolicy } = require('./model-policy.cjs');
const { loadAvailableModels } = require('./model-catalog.cjs');

const SPAWN_NAMES = new Set(['spawn_agent', 'Agent', 'collaborationspawn_agent',
  'collaboration.spawn_agent', 'multi_agent_v1spawn_agent', 'multi_agent_v1__spawn_agent']);
const WAIT_NAMES = new Set(['wait_agent', 'collaborationwait_agent', 'collaboration.wait_agent',
  'multi_agent_v1wait_agent', 'multi_agent_v1__wait_agent']);
const V2_MESSAGE_NAMES = new Set(['followup_task','send_message','collaborationfollowup_task','collaborationsend_message',
  'collaboration.followup_task','collaboration.send_message','multi_agent_v2__followup_task','multi_agent_v2__send_message']);
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const LIMIT = 1024 * 1024;
const TURN_LIMIT = 16 * 1024 * 1024;

function deny(code, message) {
  return { hookSpecificOutput: { hookEventName: 'PreToolUse', permissionDecision: 'deny',
    permissionDecisionReason: `[subagent-model-guard/${code}] ${message}` } };
}

function warning(message) {
  // Post-call feedback must not hide the created agent ID or invite duplicate spawning.
  return { systemMessage: `[subagent-model-guard] ${message}` };
}

function dataRoot(options) {
  return options.dataDir || process.env.PLUGIN_DATA || path.join(
    process.env.CODEX_HOME || path.join(os.homedir(), '.codex'), 'plugins', 'data', 'subagent-model-guard');
}

function appendAudit(record, options) {
  const root = dataRoot(options);
  fs.mkdirSync(root, { recursive: true });
  fs.appendFileSync(path.join(root, 'audit.jsonl'), JSON.stringify({ schemaVersion: 1,
    pluginVersion: require('../package.json').version,
    timestamp: new Date().toISOString(), ...record }) + '\n', { mode: 0o600 });
}

function agentRecordPath(id, options) {
  if (!UUID.test(String(id))) return null;
  return path.join(dataRoot(options), 'agents', id.toLowerCase() + '.json');
}

function loadRequest(id, options) {
  const file = agentRecordPath(id, options);
  if (!file) return null;
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function parseRecords(text) {
  const records = [];
  for (const line of text.split('\n')) {
    try {
      const record = JSON.parse(line);
      if (['session_meta', 'turn_context', 'event_msg'].includes(record.type)) records.push(record);
    } catch { /* A bounded read can split the first or last record. */ }
  }
  return records;
}

function transcriptRecords(file, limit = LIMIT) {
  let fd;
  try {
    fd = fs.openSync(file, 'r');
    const size = fs.fstatSync(fd).size;
    const head = Buffer.alloc(size <= limit ? size : Math.min(size, LIMIT));
    fs.readSync(fd, head, 0, head.length, 0);
    if (size <= limit) {
      const records = parseRecords(head.toString('utf8'));
      records.complete = true;
      return records;
    }
    const tail = Buffer.alloc(Math.min(size, limit));
    fs.readSync(fd, tail, 0, tail.length, size - tail.length);
    const first = parseRecords(head.toString('utf8'));
    const records = [...first, ...parseRecords(tail.toString('utf8'))];
    records.tailStart = first.length;
    return records;
  } catch { return []; }
  finally { if (fd !== undefined) fs.closeSync(fd); }
}

function parentProvider(input) {
  if (!UUID.test(String(input?.session_id)) || !UUID.test(String(input?.turn_id)) || !input?.transcript_path) return null;
  const records = transcriptRecords(input.transcript_path, TURN_LIMIT);
  const meta = records.find(r => r.type === 'session_meta')?.payload;
  if (!meta || meta.id !== input.session_id) return null;
  const recent = records.complete ? records : records.slice(records.tailStart || 0);
  const started = recent.findLastIndex(r => r.type === 'event_msg' && r.payload?.type === 'task_started' && r.payload?.turn_id === input.turn_id);
  if (started < 0) return null;
  const settings = recent.slice(0, started).findLast(r => r.type === 'event_msg' && r.payload?.type === 'thread_settings_applied');
  if (!settings || settings.payload.thread_id !== input.session_id) return null;
  const provider = settings.payload.thread_settings?.model_provider_id;
  return typeof provider === 'string' && provider ? provider : null;
}

function senderContext(input) {
  if (!UUID.test(String(input?.session_id)) || !UUID.test(String(input?.turn_id)) || !input?.transcript_path) return null;
  const records=transcriptRecords(input.transcript_path, TURN_LIMIT);
  const meta=records.find(r=>r.type==='session_meta')?.payload;
  if (!meta || !UUID.test(String(meta.id)) || (meta.id!==input.session_id && meta.session_id!==input.session_id)) return null;
  const recent=records.complete ? records : records.slice(records.tailStart || 0);
  const turn=recent.findLast(r=>r.type==='turn_context' && r.payload?.turn_id===input.turn_id)?.payload;
  if (!turn) return null;
  return {parentId:meta.id,agentPath:meta.agent_path || '/root',version:turn.multi_agent_version || null};
}

function isV2MessageTool(name) { return V2_MESSAGE_NAMES.has(name); }

function childEvidence(id, input, options) {
  if (!UUID.test(String(id))) return null;
  const root = options.sessionsRoot || path.join(process.env.CODEX_HOME || path.join(os.homedir(), '.codex'), 'sessions');
  const dirs = new Set(input.transcript_path ? [path.dirname(input.transcript_path)] : []);
  if (input.agent_transcript_path) dirs.add(path.dirname(input.agent_transcript_path));
  // UUIDv7 timestamps locate nearby date folders; transcript metadata still proves identity.
  const ms = Number.parseInt(id.replaceAll('-', '').slice(0, 12), 16);
  for (const delta of [-86400000, 0, 86400000]) {
    const date = new Date(ms + delta);
    if (Number.isNaN(date.getTime())) continue;
    dirs.add(path.join(root, String(date.getUTCFullYear()), String(date.getUTCMonth() + 1).padStart(2, '0'), String(date.getUTCDate()).padStart(2, '0')));
  }
  for (const dir of dirs) {
    let names;
    try { names = fs.readdirSync(dir); } catch { continue; }
    for (const name of names) {
      if (!name.endsWith('.jsonl') || !name.includes(id)) continue;
      const records = transcriptRecords(path.join(dir, name));
      const meta = records.find(r => r.type === 'session_meta')?.payload;
      if (!meta || meta.id !== id) continue;
      const parent = meta.parent_thread_id || meta.source?.subagent?.thread_spawn?.parent_thread_id;
      if (options.parentThreadId && parent !== options.parentThreadId) continue;
      const recent = records.complete ? records : records.slice(records.tailStart || 0);
      const turn = recent.filter(r => r.type === 'turn_context').at(-1)?.payload;
      if (turn && typeof turn.model === 'string') {
        return { model: turn.model, effort: turn.effort || turn.reasoning_effort || null, source: 'turn_context' };
      }
    }
  }
  return null;
}

function launchEvidence(input) {
  const callId = input.tool_use_id || input.tool_call_id;
  if (!callId || !input.transcript_path) return null;
  for (const record of transcriptRecords(input.transcript_path).reverse()) {
    const item = record.payload?.item;
    if (item?.type === 'SubAgentActivity' && item.kind === 'started' && item.id === callId && UUID.test(item.agent_thread_id)) {
      return { id: item.agent_thread_id, model: item.model || null,
        effort: item.reasoning_effort || null, source: 'launch_event' };
    }
  }
  return null;
}

function parentThread(input) {
  return transcriptRecords(input.transcript_path).find(r => r.type === 'session_meta')?.payload?.id || input.session_id || null;
}

function checkActual(id, request, input, options, launch = null) {
  const actual = childEvidence(id, input, { ...options, parentThreadId: request.parentThreadId }) || launch;
  const matches = actual && actual.model === request.model && actual.effort === request.effort;
  const status = !actual || !actual.effort ? 'unverified' : matches ? 'matched' : 'mismatch';
  appendAudit({ event: 'runtime.checked', childId: id, callId: request.callId,
    requested: { model: request.model, effort: request.effort },
    actual: actual ? { model: actual.model, effort: actual.effort, source: actual.source } : null, status }, options);
  if (status === 'matched') return null;
  return warning(status === 'mismatch'
    ? `实际模型或推理强度与请求不一致（子代理 ${id}）。先检查已有任务和副作用，再处理；不要自动重建。`
    : `子代理 ${id} 的运行设置暂未取得完整证据；保留现有任务，后续等待或结束时复核。`);
}

// Local preflight has no audit side effects; handleHook records the final decision.
function validateSpawn(toolInput, options = {}) {
  if (options.disabledModels?.has(toolInput?.model)) {
    return {ok:false, code:'MODEL_DISABLED',
      message:'此模型连接已手动停用，已跳过调度及线路可用性检查；请选择未停用的授权模型。',
      model:toolInput.model, effort:typeof toolInput.reasoning_effort === 'string' ? toolInput.reasoning_effort : null};
  }
  try {
    const policy = options.policy || loadPolicy();
    let available = options.availableModels || (options.loadAvailableModels || loadAvailableModels)(options);
    if (options.disabledModels && available?.models && typeof available.models === 'object' && !Array.isArray(available.models)) {
      available = require('./disabled-models.cjs').filterDisabledModels(available, options.disabledModels);
    }
    const result = validateSelection(toolInput, policy, available);
    const binding = require('./model-policy.cjs').hubBinding(available, toolInput?.model);
    return binding ? {...result, hubBinding:binding} : result;
  } catch (error) {
    return {ok:false, code:error.code === 'MODEL_CATALOG_UNAVAILABLE' ? error.code : 'MODEL_GUARD_UNAVAILABLE',
      message:error.code === 'MODEL_CATALOG_UNAVAILABLE' ? error.message
        : '模型校验或本地审计不可用，创建已拒绝；修复策略文件或数据目录后再试。', model:null, effort:null};
  }
}

function handleHook(input, options = {}) {
  const isSpawn = SPAWN_NAMES.has(input?.tool_name);
  const isWait = WAIT_NAMES.has(input?.tool_name);
  const isMessage = isV2MessageTool(input?.tool_name);
  const event = input?.hook_event_name;
  if (!(isSpawn && ['PreToolUse', 'PostToolUse'].includes(event)
      || isWait && event === 'PostToolUse' || isMessage && event === 'PreToolUse'
      || ['SubagentStart', 'SubagentStop'].includes(event))) return null;
  try {
    if (event === 'PreToolUse') {
      if(isMessage) {
        if (!['v1','v2'].includes(options.agentVersion)) return deny('MESSAGE_CONTEXT_UNKNOWN','无法确认消息发送方协议版本及正文交付方式，已拒绝发送；请先核对已有任务。');
        if (options.agentVersion === 'v2' && (!options.targetModel || options.targetHub || options.targetUnavailable
            || !options.targetOfficial)) {
          const code = 'HUB_V2_FOLLOWUP_UNSUPPORTED';
          appendAudit({event:'message.checked',model:options.targetModel || null,status:'denied',code},options);
          return deny(code,'该 V2 子代理目标或追加消息正文交付尚无法可靠确认，已拒绝发送；请先核对已有任务与副作用，再决定是否重新委派。');
        }
        return null;
      }
      const result = options.spawnValidation || validateSpawn(input.tool_input, options);
      if (result.ok && result.hubBinding && options.hubVerified !== true) {
        appendAudit({event:'spawn.checked',model:result.model,effort:result.effort,status:'denied',code:'HUB_ROUTE_UNVERIFIED'},options);
        return deny('HUB_ROUTE_UNVERIFIED','该模型尚未完成当前绑定及父会话出口预检，已拒绝创建。');
      }
      try {
        appendAudit({ event: 'spawn.checked', sessionId: input.session_id || null,
          callId: input.tool_use_id || input.tool_call_id || null,
          model: result.model, effort: result.effort, status: result.ok ? 'allowed' : 'denied', code: result.code }, options);
      } catch (error) {
        if (result.ok) throw error; // An audit failure must never permit a spawn or mask a known local rejection.
      }
      return result.ok ? null : deny(result.code, result.message);
    }
    if (event === 'SubagentStart') {
      let auditWarning=null;
      try {
        appendAudit({ event: 'subagent.started', childId: UUID.test(String(input.agent_id)) ? input.agent_id : null,
          actualModel: typeof input.model === 'string' ? input.model : null, source: 'SubagentStart',
          taskDelivery: options.taskRecovery?.status || null }, options);
      } catch {
        // A logging failure must not discard an already recovered task or the
        // instruction that prevents a child from guessing an undelivered task.
        auditWarning=warning('本地模型审计暂不可用；保留当前任务并检查运行状态。');
      }
      if (options.taskRecovery?.status === 'unavailable') {
        return {...(auditWarning || {}),hookSpecificOutput:{hookEventName:'SubagentStart',additionalContext:
          '系统未能可靠确认并转交本子代理的任务正文。请不要猜测或执行操作；直接向父代理说明任务正文未送达，请其接管或重新委派。'}};
      }
      let advisory = false;
      try {
        const policy = options.policy || loadPolicy();
        advisory = policy.jevAdvisory?.enabled === true;
      } catch { /* Advisory failure must not block or affect task recovery. */ }
      const guidance = advisory ? '按需辅助：若复杂任务完成复核或批量文本分类确有价值且 Jev 结构化判断工具可用，可用于第二意见；不要对每个任务强制调用。发送前只提供任务所需且获准外发的最少非敏感事实，不发送密钥或无关私人资料；供应商和模型由用户配置决定，调用可能计费。Jev 结果仅作参考，不代替原始证据、实际测试、权限判断或模型守卫。工具不可用或失败时继续现有流程。' : '';
      const recovered = options.taskRecovery?.status === 'restored'
        ? '以下是父代理为本子代理提交的原始任务正文；请在既有权限内执行并向父代理报告：\n' + options.taskRecovery.message : '';
      const context = [recovered, guidance].filter(Boolean).join('\n\n');
      return context ? {...(auditWarning || {}),hookSpecificOutput:{hookEventName:'SubagentStart',additionalContext:context}} : auditWarning;
    }
    if (event === 'SubagentStop') {
      const request = loadRequest(input.agent_id, options);
      return request ? checkActual(input.agent_id, request, input, options) : null;
    }
    if (isWait) {
      let feedback = null;
      // Both v1 UUID targets and v2 path targets are accepted by the host; only UUIDs are proven here.
      for (const id of input.tool_input?.targets || []) {
        const request = loadRequest(id, options);
        if (request) feedback = checkActual(id, request, input, options) || feedback;
      }
      return feedback;
    }
    let response = input.tool_response;
    if (typeof response === 'string') { try { response = JSON.parse(response); } catch { response = null; } }
    const launch = launchEvidence(input);
    const id = UUID.test(String(response?.agent_id)) ? response.agent_id : launch?.id;
    if (!id) {
      appendAudit({ event: 'spawn.result', callId: input.tool_use_id || input.tool_call_id || null,
        status: 'unverified', reason: 'child_identity_unavailable' }, options);
      return warning('创建返回值尚不能关联到子代理 UUID，未声称完成实际模型核对；不要重复创建。');
    }
    const request = { childId: id, callId: input.tool_use_id || input.tool_call_id || null,
      parentThreadId: parentThread(input), model: input.tool_input?.model || null,
      effort: input.tool_input?.reasoning_effort || null };
    const file = agentRecordPath(id, options);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, JSON.stringify(request), { mode: 0o600 });
    return checkActual(id, request, input, options, launch);
  } catch (error) {
    if (event === 'PreToolUse' && error.code === 'MODEL_CATALOG_UNAVAILABLE') {
      try { appendAudit({event:'spawn.checked',callId:input.tool_use_id || input.tool_call_id || null,
        model:null,effort:null,status:'denied',code:error.code},options); } catch { /* Denial remains effective without a log. */ }
      return deny(error.code, error.message);
    }
    return event === 'PreToolUse'
      ? deny('MODEL_GUARD_UNAVAILABLE', '模型校验或本地审计不可用，创建已拒绝；修复策略文件或数据目录后再试。')
      : warning('本地模型审计暂不可用；创建后的操作无法撤销，保留任务并检查状态。');
  }
}

module.exports = { validateSpawn, handleHook, parentProvider, parentThread, senderContext, isV2MessageTool, childEvidence, launchEvidence };
