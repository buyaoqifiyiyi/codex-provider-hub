'use strict';

// Preferences are advisory. Authorization and catalog checks remain in model-policy.
const path = require('node:path');
const { validateSpawn, eligibleModels, loadPolicy } = require('./model-policy.cjs');
const { persistence } = require('./state-persistence.cjs');

// Keep the exported preference shape without inventing user choices.
const DEFAULTS = Object.freeze(Object.fromEntries(['bounded','boundary','critical'].map(task =>
  [task, Object.freeze({model:'', effort:'', fallback_model:'', fallback_effort:''})])));
const TASKS = Object.keys(DEFAULTS);
const FIELDS = ['model','effort','fallback_model','fallback_effort'];
const EFFORTS = new Set(['none','minimal','low','medium','high','xhigh','max','ultra']);

function capability(model, available = {models:{}}) {
  // The user decides whether an enabled model is satisfactory. Public scores
  // and small samples are references, not model/connection authorization.
  let critical_eligible = false;
  try { critical_eligible = Object.hasOwn(eligibleModels(loadPolicy(), available), model); }
  catch (error) {
    if (error.code === 'MODEL_POLICY_INVALID' || error.code === 'MODEL_POLICY_UNAVAILABLE')
      return {capability:'unknown',critical_eligible:false,capability_reason:'模型策略无效或无法读取，暂不可选。'};
    // An unreadable catalog never grants eligibility.
  }
  return {capability:'unknown',critical_eligible,capability_reason:critical_eligible
    ? '已启用的模型可参与关键任务；实际派遣仍按偏好、线路状态及守卫检查。'
    : '当前模型不可选，请检查连接及可用强度。'};
}

function preferencePath() {
  if (!process.env.LOCALAPPDATA) throw new Error('找不到本机连接数据目录。');
  return path.join(process.env.LOCALAPPDATA, 'CodexProviderHub', 'scheduling.json');
}

function validatePreferences(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) ||
      Object.keys(value).length !== TASKS.length || !TASKS.every(t => Object.hasOwn(value,t)))
    throw new Error('调度偏好格式不正确。');
  const clean = {};
  for (const task of TASKS) {
    const row = value[task];
    if (!row || typeof row !== 'object' || Array.isArray(row) ||
        Object.keys(row).length !== FIELDS.length || !FIELDS.every(f => typeof row[f] === 'string'))
      throw new Error('调度偏好必须包含首选与备选模型及强度。');
    for (const [model, effort] of [[row.model,row.effort],[row.fallback_model,row.fallback_effort]]) {
      if (!/^[A-Za-z0-9_.:/~-]{1,160}$/.test(model) || !EFFORTS.has(effort))
        throw new Error('模型名称或推理强度不符合格式要求。');
    }
    clean[task] = Object.fromEntries(FIELDS.map(f => [f,row[f]]));
  }
  return clean;
}

function readRoutingSnapshot() {
  const value=persistence('read',{root:path.dirname(preferencePath())});
  if (!value || !['auto','manual'].includes(value.mode) || typeof value.saved!=='boolean')
    throw new Error('本机调度状态无法读取。');
  const preferences=value.preferences === null ? structuredClone(DEFAULTS) : validatePreferences(value.preferences);
  return {mode:value.mode,preferences,saved:value.saved};
}

function readPreferences() { return readRoutingSnapshot().preferences; }
function preferencesSaved() { return readRoutingSnapshot().saved; }

function readRoutingMode() { return readRoutingSnapshot().mode; }

// All writers use the same SQLite mutex. Mode read/merge/replace happens
// entirely in the helper transaction, never from a caller-side old snapshot.
function saveRoutingMode(mode) {
  if (!['auto','manual'].includes(mode)) throw new Error('调度模式无效。');
  return persistence('mode', {root:path.dirname(preferencePath()),mode});
}

function saveManualRouting(value) {
  const preferences=validatePreferences(value);
  return persistence('manual', {root:path.dirname(preferencePath()),preferences});
}

function saveRoutingPreferences(value) {
  const preferences=validatePreferences(value);
  return persistence('preferences', {root:path.dirname(preferencePath()),preferences});
}

function savePreferences(value) {
  const preferences=validatePreferences(value);
  return persistence('legacy', {root:path.dirname(preferencePath()),preferences});
}

function select(task, preferences, available, requested = null, options = {}) {
  if (!TASKS.includes(task)) return {ok:false,code:'TASK_TYPE_INVALID',reason:'请选择有效的任务类型。'};
  const purpose = options.purpose || 'text';
  if (!['text','tool','image','video','speech','audio'].includes(purpose)) return {ok:false,code:'PURPOSE_INVALID',reason:'请选择有效的任务用途。'};
  let policy;
  try { policy=loadPolicy(); }
  catch (error) {
    return {ok:false,code:error.code || 'MODEL_POLICY_UNAVAILABLE',reason:'模型策略无效或无法读取，已停止调度。'};
  }
  const evidence = options.evidence && typeof options.evidence === 'object' ? options.evidence : {};
  const excluded = Array.isArray(options.excluded) ? options.excluded : [];
  const temporarilyExcluded = (model, effort) => excluded.some(item => item && item.model === model && item.effort === effort);
  function purposeStatus(model, effort) {
    const records = Array.isArray(evidence[model]) ? evidence[model] : [];
    const matching = records.filter(r => r && ['passed','failed','error'].includes(r.status) && r.purpose === purpose &&
      r.channel === 'subagent_gateway' && r.effort === effort && !Number.isNaN(Date.parse(r.tested_at)))
      .sort((a,b) => Date.parse(b.tested_at)-Date.parse(a.tested_at));
    return ({passed:'sample_passed',failed:'sample_failed',error:'test_error'})[matching[0]?.status] || 'pending_test';
  }
  if (!requested && options.mode !== 'manual') {
    const references=options.references && typeof options.references === 'object' ? options.references : {};
    const eligible=eligibleModels(policy,available);
    const saved=options.preferencesSaved === true || (options.mode === undefined && options.preferencesSaved !== false)
      ? preferences?.[task] : null;
    const relevance=purpose==='image' ? /vision|multimodal|image|mmmu|charxiv/i
      : purpose==='tool' ? /swe|terminal.?bench|tool|function|agent/i
      : task==='critical' || task==='boundary' ? /hle|mmlu|gpqa|reasoning/i : /swe|terminal.?bench|mmlu|hle|gpqa/i;
    const comparable=/^(?:[^/]+\/)?(?:terminal-bench(?:\s+\d+(?:\.\d+)*)?|swe-bench pro|deep-swe|hle)$/i;
    const reasons=[];
    const candidates=[];
    for (const [model, permitted] of Object.entries(eligible)) {
      const configured=[saved?.model===model ? saved.effort : null,
                        saved?.fallback_model===model ? saved.fallback_effort : null].filter(Boolean);
      const choices=[...new Set([...configured,...(options.requiredEffort ? [options.requiredEffort] : permitted)])].filter(e=>permitted.includes(e) &&
        (!options.requiredEffort || e===options.requiredEffort));
      const configuredOrder=effort=>configured.includes(effort) ? configured.indexOf(effort) : configured.length;
      choices.sort((a,b)=>configuredOrder(a)-configuredOrder(b) ||
        Number(purposeStatus(model,a)!=='sample_passed')-Number(purposeStatus(model,b)!=='sample_passed'));
      const effort=choices.find(e=>!temporarilyExcluded(model,e) &&
        (purpose==='text' || !['sample_failed','test_error'].includes(purposeStatus(model,e))) &&
        validateSpawn({model,reasoning_effort:e},policy,available).ok);
      if (!effort) { reasons.push({model,code:'PURPOSE_OR_EFFORT_UNAVAILABLE'});continue; }
      const ref=references[model];
      const rows=ref?.status==='available' && Array.isArray(ref.results) ? ref.results : [];
      const metrics=new Map();
      for (const row of rows) {
        if (!row || typeof row.benchmark!=='string' || !relevance.test(row.benchmark) || !comparable.test(row.benchmark)
            || typeof row.task_id!=='string' || !row.task_id || row.unit!=='%'
            || typeof row.conditions_fingerprint!=='string' || !/^[a-f0-9]{64}$/.test(row.conditions_fingerprint)
            || typeof row.score!=='number' || !Number.isFinite(row.score) || row.score<0 || row.score>100) continue;
        const key=JSON.stringify([row.benchmark.toLowerCase(),row.task_id.toLowerCase(),row.unit,row.conditions_fingerprint]);
        metrics.set(key,metrics.has(key) && metrics.get(key)!==row.score ? null : row.score);
      }
      const related=rows.some(row=>typeof row?.benchmark==='string' && relevance.test(row.benchmark));
      const passed=purpose!=='text' && purposeStatus(model,effort)==='sample_passed';
      const preference=saved?.model===model && (effort===saved.effort || !!options.requiredEffort) ? 2
        : saved?.fallback_model===model && (effort===saved.fallback_effort || !!options.requiredEffort) ? 1 : 0;
      candidates.push({model,effort,passed,related,metrics,preference,comparisonHigher:false});
    }
    // Catalog order breaks ties without assigning a supplier or model prior.
    candidates.sort((a,b)=>b.preference-a.preference || Number(b.passed)-Number(a.passed) ||
      (purpose==='image' ? Number(b.related)-Number(a.related) : 0));
    let selected=candidates[0];
    if (!selected) return {ok:false,code:'NO_ELIGIBLE_MODEL',reason:'当前没有符合任务条件的已启用模型。',skipped:reasons};

    // Each challenger must beat the baseline without conflicting results on
    // any benchmark they share. Missing scores from a third model cannot hide
    // a disagreement between the baseline and that challenger.
    const compare=(a,b)=>{
      const shared=[...a.metrics.keys()].filter(key=>typeof a.metrics.get(key)==='number' && typeof b.metrics.get(key)==='number');
      if (!shared.length) return null;
      const directions=new Set(shared.map(key=>Math.sign(a.metrics.get(key)-b.metrics.get(key))).filter(Boolean));
      return directions.size>1 ? null : directions.values().next().value || 0;
    };
    const challengers=candidates.filter(candidate=>candidate!==selected && compare(candidate,selected)===1);
    const winners=challengers.filter(candidate=>challengers.every(other=>candidate===other ||
      (compare(candidate,other)!==null && compare(candidate,other)>=0)));
    if (winners.length) {
      selected=winners[0];
      selected.comparisonHigher=true;
    }
    const selection_reasons=[];
    if (selected.preference===2) selection_reasons.push('参考已保存的首选');
    else if (selected.preference===1) selection_reasons.push('参考已保存的备选');
    if (selected.passed) selection_reasons.push('用途短测通过');
    if (selected.comparisonHigher) selection_reasons.push('相同条件的可比公开评测较高');
    else if (selected.related) selection_reasons.push(purpose==='image' ? '有图片相关公开评测' : '有任务相关公开评测');
    if (!selection_reasons.length) selection_reasons.push('按任务类型及可用模型选择');
    return {ok:true,model:selected.model,effort:selected.effort,source:'automatic',purpose,
      purpose_status:purposeStatus(selected.model,selected.effort),selection_reasons,skipped:reasons};
  }
  const row = preferences?.[task] || DEFAULTS[task];
  const choices = requested ? [[requested.model, requested.effort, 'explicit']]
    : [[row.model,row.effort,'preferred'], [row.fallback_model,row.fallback_effort,'fallback']];
  const reasons=[];
  for (const [model, effort, source] of choices) {
    if (options.requiredEffort && options.requiredEffort !== effort) {
      reasons.push({model,effort,code:'MODEL_EFFORT_UNSUPPORTED'}); continue;
    }
    if (temporarilyExcluded(model,effort)) {
      reasons.push({model,effort,code:'SAMPLE_TEMPORARILY_EXCLUDED'}); continue;
    }
    const result = validateSpawn({model,reasoning_effort:effort},policy,available);
    if (result.ok && !requested && purpose !== 'text' && ['sample_failed','test_error'].includes(purposeStatus(model,effort))) {
      reasons.push({model,effort,code:purposeStatus(model,effort) === 'sample_failed' ? 'CAPABILITY_SAMPLE_FAILED' : 'CAPABILITY_TEST_ERROR'}); continue;
    }
    if (result.ok) return {ok:true,model,effort,source,purpose,purpose_status:purposeStatus(model,effort),skipped:reasons};
    reasons.push({model,effort,code:result.code});
  }
  return {ok:false,code:requested ? 'EXPLICIT_MODEL_UNAVAILABLE' : 'NO_ELIGIBLE_MODEL',
    reason:requested ? '明确指定的模型或强度当前不可用，未自动换用其他模型。'
      : options.mode==='manual' ? '手动设置的首选和备选不可用，未自动换用其他模型。'
      : '首选、备选及符合任务条件的自动候选当前均不可用。', skipped:reasons};
}

module.exports = { DEFAULTS, TASKS, readPreferences, savePreferences, preferencesSaved, readRoutingMode, readRoutingSnapshot, saveRoutingMode, saveManualRouting, saveRoutingPreferences, select, capability };
