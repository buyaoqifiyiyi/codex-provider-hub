#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { loadAvailableModels } = require('../lib/model-catalog.cjs');
const { loadDisabledModels } = require('../lib/disabled-models.cjs');
const { eligibleModels, loadPolicy } = require('../lib/model-policy.cjs');
const { readRoutingSnapshot, saveManualRouting, saveRoutingPreferences, saveRoutingMode, select, capability } = require('../lib/scheduling.cjs');

function safe(value) { return typeof value === 'string' ? value.slice(0,200) : null; }
function recentAudit() {
  const home = process.env.CODEX_HOME || path.join(os.homedir(),'.codex');
  const root = process.env.PLUGIN_DATA || path.join(home,'plugins','data','codex-provider-hub-codex-provider-hub-local');
  let fd;
  try {
    fd=fs.openSync(path.join(root,'audit.jsonl'),'r');
    const size=fs.fstatSync(fd).size, length=Math.min(size,256*1024);
    const buf=Buffer.alloc(length); fs.readSync(fd,buf,0,length,size-length);
    return buf.toString('utf8').split('\n').reverse().flatMap(line => {
      try {
        const r=JSON.parse(line);
        if (!['spawn.checked','runtime.checked','spawn.result'].includes(r.event)) return [];
        return [{time:safe(r.timestamp),event:safe(r.event),model:safe(r.model || r.actual?.model || r.requested?.model),
          effort:safe(r.effort || r.actual?.effort || r.requested?.effort),result:safe(r.status),reason:safe(r.code || r.reason),
          requested:r.requested ? {model:safe(r.requested.model),effort:safe(r.requested.effort)} : null,
          actual:r.actual ? {model:safe(r.actual.model),effort:safe(r.actual.effort)} : null}];
      } catch { return []; }
    }).slice(0,20);
  } catch (e) { if(e.code === 'ENOENT') return []; throw e; }
  finally { if(fd !== undefined) fs.closeSync(fd); }
}

function overview(providers = [], options = {}) {
  // These are local metadata reads only. In particular, no disabled line is probed.
  const disabled=loadDisabledModels(), catalog=loadAvailableModels();
  const eligible=eligibleModels(loadPolicy(),catalog);
  const localModels=Object.fromEntries(Object.entries(eligible).filter(([model])=>!disabled.has(model)));
  const usableCatalog={...catalog,models:localModels};
  const connectionName = binding => safe(providers.find(p=>p.id===binding?.connection)?.name);
  const seen=new Set();
  const candidates=Object.entries(catalog.models).map(([model,efforts]) => {
    seen.add(model);
    const bound=catalog.hubModels?.[model];
    const registered=!!bound || [catalog.nativeModels,catalog.managedOfficialModels]
      .some(models=>Array.isArray(models) && models.includes(model));
    const state=disabled.has(model) ? 'disabled' : eligible[model] ? 'available'
      : registered && !efforts.length ? 'effort_unknown' : 'outside_policy';
    const displayName=safe(bound?.model) || model;
    return {model,display_name:displayName,efforts:eligible[model] || [],state,...capability(model,usableCatalog),
      connection:connectionName(bound),context_window:catalog.modelDetails?.[model]?.context_window || null,
      reason:state==='available'
        ? '模型在本机目录中；实际线路状态尚未检测。' : state==='disabled' ? '连接已停用。'
        : state==='effort_unknown' ? '已启用，但思考强度未确认；可在连接高级选项中设置。' : '尚未启用该模型连接或没有可用的强度。'};
  });
  for(const model of disabled) if(!seen.has(model)) { seen.add(model); candidates.push({model,efforts:[],state:'disabled',...capability(model),reason:'连接已停用。'}); }
  const saved = new Map();
  for (const profile of providers) for (const model of profile.models || []) {
    if (profile.id && Object.values(catalog.hubModels || {}).some(b=>b.connection===profile.id && b.model===model)) continue;
    if (!saved.has(model)) saved.set(model,[]);
    saved.get(model).push(profile);
  }
  for(const [model,profiles] of saved) {
    if(seen.has(model)) continue;
    seen.add(model);
    const state=profiles.every(p=>p.disabled) ? 'disabled' : 'not_in_catalog';
    const labels=profiles.map(p=>`${safe(p.name) || '未命名连接'}（${p.disabled ? '已停用' : '已保存'}）`).join('、');
    const reason=state==='disabled' ? '连接均已停用。' : '尚未作为子代理模型接入当前目录；启用后以连接别名参与调度。';
    candidates.push({model,efforts:[],state,...capability(model),reason:`${labels}：${reason}`});
  }
  const snapshot=readRoutingSnapshot(),preferences=snapshot.preferences,routingMode=snapshot.mode;
  const effective=Object.fromEntries(Object.keys(preferences).map(task=>{
    const result=select(task,preferences,{...catalog,models:localModels},null,
      {mode:routingMode,preferencesSaved:snapshot.saved,automaticProviderFirst:!snapshot.saved,evidence:options.evidence,references:options.references});
    for (const item of result.skipped || []) if (disabled.has(item.model)) item.code='MODEL_DISABLED';
    if (result.ok) result.connection=connectionName(catalog.hubModels?.[result.model]);
    return [task,result];
  }));
  return {ok:true,preferences,preferences_saved:snapshot.saved,routing_mode:routingMode,effective,candidates,recent:recentAudit(),
    notice:routingMode==='manual' ? '手动调度按已保存的首选和备选选择；实际创建仍由宿主及守卫校验。'
      : '自动选择参考任务、已保存偏好和已有证据；实际创建仍由宿主及守卫校验。'};
}

async function recommend(task, requested, dependencies = {}, options = {}) {
  const deadline=(dependencies.now || Date.now)()+24000;
  const snapshot=readRoutingSnapshot(),prefs=snapshot.preferences;
  if (!Object.hasOwn(prefs,task)) return {ok:false,code:'TASK_TYPE_INVALID',reason:'请选择有效的任务类型。'};
  const result=await require('./list-models.cjs').listModels(dependencies);
  const selection=await require('./list-models.cjs').recommendFromSnapshot(task,prefs,result,requested,dependencies,
    {mode:snapshot.mode,preferencesSaved:snapshot.saved,automaticProviderFirst:!snapshot.saved,deadline,purpose:options.purpose,
      evidence:options.evidence,references:options.references,excluded:options.excluded});
  for (const item of selection.skipped || []) {
    if (result.disabledModels.includes(item.model)) { item.code='MODEL_DISABLED'; continue; }
  }
  return {...selection,routing_mode:snapshot.mode,route_status:result.routeStatus,
    note:'推荐基于当前目录和已知线路状态，创建时仍需守卫校验。'};
}

async function run(action,args) {
  if(action==='overview') return overview(args.providers,args);
  if(action==='save') {
    if (args.mode==='manual') {
      const preferences=saveManualRouting(args.preferences);
      return {ok:true,preferences,routing_mode:'manual'};
    }
    if (args.mode && args.mode!=='current') throw new Error('调度模式无效。');
    const saved=saveRoutingPreferences(args.preferences);
    return {ok:true,preferences:saved.preferences,routing_mode:saved.mode};
  }
  if(action==='mode') {
    try { return {ok:true,routing_mode:saveRoutingMode(args.mode)}; }
    catch(error) {
      if(error.code==='SCHEDULING_PREFERENCES_REQUIRED') return {ok:false,error:'请先保存首选和备选偏好。'};
      throw error;
    }
  }
  if(action==='recommend') return recommend(args.task,args.requested || null,{},args);
  throw new Error('未知调度操作。');
}
if(require.main===module) {
  let input=''; process.stdin.setEncoding('utf8');
  process.stdin.on('data',chunk=>{input+=chunk; if(input.length>1024*1024) process.exit(2);});
  process.stdin.on('end',()=>Promise.resolve().then(()=>run(process.argv[2],input ? JSON.parse(input) : {}))
    .then(value=>process.stdout.write(JSON.stringify(value)+'\n'))
    .catch(error=>process.stdout.write(JSON.stringify({ok:false,error:safe(error.message) || '调度操作失败。'})+'\n')));
}
module.exports = { overview, recommend, run, recentAudit };
