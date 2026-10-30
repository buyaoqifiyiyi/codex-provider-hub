#!/usr/bin/env node
'use strict';
const { loadAvailableModels } = require('../lib/model-catalog.cjs');
const { eligibleModels, loadPolicy } = require('../lib/model-policy.cjs');
const { parentProvider, senderContext } = require('../lib/guard.cjs');
const { resolveParentRoute, currentSession } = require('../lib/parent-route.cjs');
const { loadDisabledModels, filterDisabledModels } = require('../lib/disabled-models.cjs');

async function listModels(dependencies = {}, options = {}) {
    const disabled = (dependencies.loadDisabledModels || loadDisabledModels)();
    const catalog = filterDisabledModels((dependencies.loadAvailableModels || loadAvailableModels)(), disabled);
    const eligible = eligibleModels(loadPolicy(), catalog);
    // Enumeration is local only; route proof is deferred to the selected candidate.
    return { source:catalog.source, disabledModels:[...disabled], models:eligible,
      hubModels:catalog.hubModels || {}, managedOfficialModels:catalog.managedOfficialModels || [],
      nativeModels:Array.isArray(catalog.nativeModels) ? catalog.nativeModels : [],
      routeStatus:'pending_dispatch' };

}

// Only recommend a third-party route after local binding and same-turn parent
// egress have been proved. Guard repeats the check at the actual spawn.
async function verifyManaged(alias, binding, dependencies = {}, deadline = Infinity) {
    const getBinding = dependencies.resolveBindingKind || require('../lib/binding-kind.cjs').resolveBindingKind;
    const now = dependencies.now || Date.now;
    try {
      if (deadline-now()<3200) return 'HUB_PRECHECK_TIMEOUT';
      const before=await getBinding(alias);
      const matches=b=>b && b.kind === 'bound' && b.revision===binding.revision
        && b.model===binding.model && b.connection===binding.connection;
      if (!matches(before)) return 'HUB_BINDING_UNAVAILABLE';
      const context=await (dependencies.currentSession || currentSession)(process.env.CODEX_THREAD_ID || process.env.CODEX_SESSION_ID);
      if (!context) return 'HUB_DISPATCH_CHECK_PENDING';
      const sender=(dependencies.senderContext || senderContext)(context);
      if (!['v1','v2'].includes(sender?.version)) return 'HUB_TASK_DELIVERY_UNSUPPORTED';
      const remaining=deadline-now();
      if (remaining<3200) return 'HUB_PRECHECK_TIMEOUT';
      const parent=await (dependencies.resolveParentRoute || resolveParentRoute)(
        (dependencies.parentProvider || parentProvider)(context), {...context,routeModel:alias,routeRevision:binding.revision,
          routeTimeoutMs:Math.min(12000,remaining-3000)});
      const after=await getBinding(alias);
      if (!matches(after) || before.kind!==after.kind) return 'HUB_BINDING_CHANGED';
      if (parent!=='compatible') return 'HUB_PARENT_ROUTE_UNKNOWN';
      return null;
    } catch { return 'HUB_BINDING_UNAVAILABLE'; }
}

async function recommendFromSnapshot(task, prefs, result, requested = null, dependencies = {}, selectionOptions = {}) {
    const scheduling=require('../lib/scheduling.cjs');
    const models={...result.models};
    const rejected=[];
    while (true) {
      const selected=scheduling.select(task,prefs,{models,hubModels:result.hubModels,
        managedOfficialModels:result.managedOfficialModels,nativeModels:result.nativeModels},requested,selectionOptions);
      selected.skipped=[...rejected,...(selected.skipped || [])];
      if (!selected.ok) return selected;
      if (!result.hubModels?.[selected.model]) return {...selected,dispatch_status:'not_checked'};
      const code=await verifyManaged(selected.model,result.hubModels[selected.model],dependencies,selectionOptions.deadline);
      if (!code) return {...selected,dispatch_status:'route_checked'};
      if (code==='HUB_DISPATCH_CHECK_PENDING') return {...selected,dispatch_status:'pending_dispatch',dispatch_code:code};
      rejected.push({model:selected.model,effort:selected.effort,code});
      delete models[selected.model];
      if (requested) return {ok:false,code:'EXPLICIT_MODEL_UNAVAILABLE',
        reason:'明确指定的模型当前线路或任务交接未能确认，未自动换用其他模型。',skipped:rejected};
    }
}

if (require.main === module) {
  const deadline=Date.now()+24000;
  listModels().then(async result => {
    const scheduling = require('../lib/scheduling.cjs');
    const snapshot = scheduling.readRoutingSnapshot(), prefs = snapshot.preferences;
    const rows=await Promise.all(scheduling.TASKS.map(async task =>
      [task, await recommendFromSnapshot(task,prefs,result,null,{}, {mode:snapshot.mode,preferencesSaved:snapshot.saved,automaticProviderFirst:!snapshot.saved,deadline})]));
    result.recommendations = Object.fromEntries(rows);
    console.log(JSON.stringify(result));
}).catch(error => {
    process.stderr.write(error.code === 'MODEL_DISABLE_STATE_UNAVAILABLE'
      ? 'MODEL_DISABLE_STATE_UNAVAILABLE: 本机连接停用状态不可读取，已停止调度。\n'
      : 'MODEL_CATALOG_UNAVAILABLE: 当前模型目录或授权策略不可用。\n');
    process.exitCode = 1;
  });
}

module.exports = { listModels, recommendFromSnapshot, verifyManaged };
