'use strict';
const path = require('node:path');
const os = require('node:os');
const { persistence } = require('./state-persistence.cjs');
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i;
const CALL = /^[A-Za-z0-9_.:-]{1,160}$/;
const EFFORTS = new Set(['none','minimal','low','medium','high','xhigh','max','ultra']);
const TTL_MS = 300000, MAX_RECORDS = 128, MAX_BYTES = 1024;
function isAutoModel(value, { allowOmitted = false } = {}) {
  return value === 'auto' || typeof value === 'string' && /^auto:(bounded|boundary|critical):(text|tool|image)$/.test(value)
    || value === undefined && allowOmitted === true;
}
function fail(code) { const e = new Error(code); e.code = code; throw e; }
function root(options) {
  const dir = options.dataDir || process.env.PLUGIN_DATA || path.join(process.env.CODEX_HOME || path.join(os.homedir(), '.codex'), 'plugins', 'data', 'subagent-model-guard');
  return dir;
}
function identity(value) {
  if (!UUID.test(String(value.parentThreadId)) || !UUID.test(String(value.turnId)) || !CALL.test(String(value.callId))) fail('AUTO_SELECTION_ID_INVALID');
  return {parentThreadId:value.parentThreadId.toLowerCase(), turnId:value.turnId.toLowerCase(), callId:value.callId};
}
function modelValid(model) { return typeof model === 'string' && /^[A-Za-z0-9_.:/~-]{1,160}$/.test(model) && model!=='auto' && !model.startsWith('auto:'); }
function clock(options) { const n = options.now ?? Date.now(); if (!Number.isSafeInteger(n) || n < 0) fail('AUTO_SELECTION_CLOCK_INVALID'); return n; }
function recordAutoSelection(input, resolved, options = {}) {
  if (!input?.tool_input || !isAutoModel(input.tool_input.model, {allowOmitted: options.allowOmitted === true && !Object.hasOwn(input.tool_input,'model')})) fail('AUTO_SELECTION_NOT_AUTO');
  const id = identity({parentThreadId:Object.hasOwn(options,'parentThreadId') ? options.parentThreadId : input.session_id,
    turnId:input.turn_id, callId:input.tool_use_id || input.tool_call_id});
  const effort = resolved?.reasoning_effort ?? resolved?.effort;
  if (!modelValid(resolved?.model) || !EFFORTS.has(effort) || resolved?.effort && resolved?.reasoning_effort && resolved.effort !== resolved.reasoning_effort) fail('AUTO_SELECTION_RESOLVED_INVALID');
  const now = clock(options);
  return Object.freeze(persistence('record', {root:root(options),record:{...id,model:resolved.model,effort},now}, 'AUTO_SELECTION_BUSY'));
}
function lookupAutoSelection(query, options = {}) {
  try {
    const id = identity(query), now = clock(options);
    if (!modelValid(query.model)) return null;
    const value=persistence('lookup', {root:root(options),record:{...id,model:query.model},now}, 'AUTO_SELECTION_BUSY');
    return value ? Object.freeze(value) : null;
  } catch { return null; }
}
module.exports = {recordAutoSelection,lookupAutoSelection,isAutoModel,TTL_MS,MAX_RECORDS,MAX_BYTES};
