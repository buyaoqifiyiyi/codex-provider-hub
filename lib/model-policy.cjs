'use strict';

const fs = require('node:fs');
const path = require('node:path');
const EFFORTS = new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
const NAME = /^[A-Za-z0-9_.:/~-]{1,160}$/;
function result(ok, code, message, model = null, effort = null) {
  return { ok, code, message, model, effort };
}

function validPolicy(policy, strict = false) {
  const object = value => value && typeof value === 'object' && !Array.isArray(value);
  const unique = values => new Set(values).size === values.length;
  const fields = ['schemaVersion','allowedFamilies','allowedEffortsByFamily','jevAdvisory'];
  if (!object(policy) || policy.schemaVersion !== 2 ||
      (strict && (Object.keys(policy).length !== fields.length || !fields.every(k => Object.hasOwn(policy,k)))) ||
      !Array.isArray(policy.allowedFamilies) || !unique(policy.allowedFamilies) ||
      !policy.allowedFamilies.every(family => typeof family === 'string' && NAME.test(family))) return false;
  const limits = policy.allowedEffortsByFamily;
  if ((strict || limits !== undefined) && (!object(limits) || !Object.entries(limits).every(([family,efforts]) =>
      NAME.test(family) && Array.isArray(efforts) && efforts.length > 0 && unique(efforts) &&
      efforts.every(effort => EFFORTS.has(effort))))) return false;
  const advisory = policy.jevAdvisory;
  if ((strict || advisory !== undefined) && (!object(advisory) || typeof advisory.enabled !== 'boolean' ||
      Object.keys(advisory).length !== 1 || !Object.hasOwn(advisory,'enabled'))) return false;
  return true;
}

// JSON.parse accepts duplicate keys. Reject them so a later permissive field
// cannot silently replace an earlier, more restrictive one.
function hasDuplicateKeys(source) {
  const stack = [];
  for (let i=0; i<source.length; i++) {
    const char=source[i];
    if (char === '"') {
      const start=i++;
      while (i<source.length && source[i] !== '"') {
        if (source[i] === '\\') i++;
        i++;
      }
      const parent=stack[stack.length-1];
      if (parent?.keys && parent.expectKey) {
        const key=JSON.parse(source.slice(start,i+1));
        if (parent.keys.has(key)) return true;
        parent.keys.add(key);
        parent.expectKey=false;
      }
    } else if (char === '{') stack.push({keys:new Set(),expectKey:true});
    else if (char === '[') stack.push({});
    else if (char === '}' || char === ']') stack.pop();
    else if (char === ',' && stack[stack.length-1]?.keys) stack[stack.length-1].expectKey=true;
  }
  return false;
}

function policyError(code) {
  const error = new Error(code === 'MODEL_POLICY_INVALID' ? '本机模型策略格式无效。' : '模型策略无法读取。');
  error.code = code;
  return error;
}

function loadPolicy() {
  if (!process.env.LOCALAPPDATA) throw policyError('MODEL_POLICY_UNAVAILABLE');
  const local = path.join(process.env.LOCALAPPDATA,'CodexProviderHub','model-policy.json');
  let source;
  try {
    source=fs.readFileSync(local,'utf8');
  } catch (error) {
    if (error.code !== 'ENOENT') throw policyError('MODEL_POLICY_UNAVAILABLE');
    // A dangling link is an existing but unreadable policy, not an absent one.
    let absent=false;
    try { fs.lstatSync(local); }
    catch (statError) {
      if (statError.code !== 'ENOENT') throw policyError('MODEL_POLICY_UNAVAILABLE');
      absent=true;
    }
    if (!absent) throw policyError('MODEL_POLICY_UNAVAILABLE');
    try { source=fs.readFileSync(path.join(__dirname,'..','policy.json'),'utf8'); }
    catch { throw policyError('MODEL_POLICY_UNAVAILABLE'); }
  }
  try {
    if (source.charCodeAt(0) === 0xfeff) source=source.slice(1);
    const value=JSON.parse(source);
    if (hasDuplicateKeys(source) || !validPolicy(value,true)) throw new Error();
    return value;
  } catch { throw policyError('MODEL_POLICY_INVALID'); }
}

// An exact family or a family followed by a separator matches. A configured
// trailing separator is an explicit prefix; dots within versions do not merge
// otherwise distinct families.
function matchesFamily(id, family) {
  return id === family || (/[-/_.:~]$/.test(family)
    ? id.startsWith(family)
    : ['-','/','_',':','~'].some(separator => id.startsWith(family+separator)));
}

function disabled(available, model) {
  const models = available?.disabledModels;
  return models instanceof Set ? models.has(model) : Array.isArray(models) && models.includes(model);
}

function hubBinding(available, model) {
  if (disabled(available, model) || !available?.models || !Object.hasOwn(available.models, model)
      || !available?.hubModels || !Object.hasOwn(available.hubModels, model)) return null;
  const b = available.hubModels[model];
  return b && b.enabled !== false && b.disabled !== true && b.kind !== 'disabled'
    && typeof b.revision === 'string' && /^[a-f0-9]{64}$/.test(b.revision)
    && typeof b.model === 'string' && b.model.trim() && b.model.trim() === b.model
    && typeof b.connection === 'string' && b.connection.trim() && b.connection.trim() === b.connection ? b : null;
}

function authorized(model, policy, available) {
  if (disabled(available, model)) return false;
  // A malformed binding cannot fall through to another authorization path.
  if (available?.hubModels && Object.hasOwn(available.hubModels, model)) return !!hubBinding(available, model);
  // Explicitly managed subscriptions are already authorized by their binding.
  if (Array.isArray(available?.managedOfficialModels) && available.managedOfficialModels.includes(model)) return true;
  // Native identity must come from the trusted catalog loader, and a local
  // allowlist narrows this path only.
  return Array.isArray(available?.nativeModels) && available.nativeModels.includes(model) &&
    (!policy.allowedFamilies.length || policy.allowedFamilies.some(family => matchesFamily(model,family)));
}

function permittedEfforts(model, efforts, policy, available) {
  // Legacy user-supplied family restrictions remain data, never built-in rules.
  // A restriction may name either a full ID or the model name behind a route.
  const ids = [model, hubBinding(available, model)?.model].filter(Boolean)
    .flatMap(id => [id,id.slice(id.lastIndexOf('/')+1)]);
  const restrictions = Object.entries(policy.allowedEffortsByFamily || {})
    // Case variants must not bypass a restriction on the underlying model.
    .filter(([family]) => ids.some(id => matchesFamily(id.toLowerCase(),family.toLowerCase())))
    .map(([, allowed]) => allowed);
  return efforts.filter(e => EFFORTS.has(e) && restrictions.every(allowed => allowed.includes(e)));
}

function eligibleModels(policy, available) {
  if (!validPolicy(policy) || !available?.models || typeof available.models !== 'object' || Array.isArray(available.models)) {
    throw new Error('Invalid authorization or model catalog');
  }
  return Object.fromEntries(Object.entries(available.models).filter(([model, efforts]) =>
    authorized(model, policy, available) && Array.isArray(efforts))
    .map(([model, efforts]) => [model, permittedEfforts(model, efforts, policy, available)])
    .filter(([, efforts]) => efforts.length > 0));
}

function candidates(policy, available) {
  return Object.entries(eligibleModels(policy, available)).slice(0, 8)
    .map(([model, efforts]) => `${model}[${efforts.join('/')}]`).join('、');
}

function validateSpawn(toolInput, policy, available) {
  if (!validPolicy(policy)) {
    return result(false, 'MODEL_POLICY_INVALID', '模型策略无效，请修正版本、模型名称或档位配置。');
  }
  if (!toolInput || typeof toolInput !== 'object' || Array.isArray(toolInput)) {
    return result(false, 'MODEL_INPUT_INVALID', '输入格式无效，请提供包含模型与推理档位的对象。');
  }

  const model = toolInput.model;
  if (!Object.hasOwn(toolInput, 'model') || typeof model !== 'string' || model.length === 0 || model.trim() !== model) {
    return result(false, 'MODEL_REQUIRED', '请提供无首尾空白的模型 ID。');
  }
  if (!authorized(model, policy, available)) {
    return result(false, 'MODEL_NOT_ALLOWED', '该模型没有可信目录身份或有效的已启用绑定，请检查当前目录与连接。');
  }
  if (!available?.models || typeof available.models !== 'object' || Array.isArray(available.models)) {
    return result(false, 'MODEL_CATALOG_UNAVAILABLE', '当前模型目录不可用，请恢复目录配置后重试。');
  }
  if (!Object.hasOwn(available.models, model) || !Array.isArray(available.models[model])) {
    const choices = candidates(policy, available);
    return result(false, 'MODEL_UNAVAILABLE', `该模型已不在当前目录中。${choices ? `请按任务改选：${choices}` : '当前没有授权范围内的候选模型，请恢复目录配置。'}`);
  }

  if (!available.models[model].length) {
    return result(false, 'MODEL_EFFORT_UNSUPPORTED', '模型已启用，但没有可确认的思考强度；请检查服务商信息或在高级选项中设置。');
  }

  const effort = toolInput.reasoning_effort;
  if (!Object.hasOwn(toolInput, 'reasoning_effort') || typeof effort !== 'string' || effort.length === 0 || effort.trim() !== effort) {
    return result(false, 'MODEL_EFFORT_REQUIRED', '请提供无首尾空白的 reasoning_effort。');
  }
  if (!EFFORTS.has(effort) || !available.models[model].includes(effort)) {
    return result(false, 'MODEL_EFFORT_UNSUPPORTED', `该模型当前支持：${available.models[model].filter(e => EFFORTS.has(e)).join('/')}。请重新选择推理强度。`);
  }
  const permitted = permittedEfforts(model, available.models[model], policy, available);
  if (!permitted.includes(effort)) {
    return result(false, 'MODEL_EFFORT_NOT_ALLOWED', `用户策略限制该模型的推理强度。${permitted.length
      ? `当前可选：${permitted.join('/')}。` : '当前目录没有符合策略的强度，请改选授权候选或恢复目录。'}`);
  }
  return result(true, 'MODEL_SELECTION_VALID', '模型与推理档位有效。', model, effort);
}

module.exports = { validateSpawn, eligibleModels, hubBinding, loadPolicy };
