'use strict';
const path = require('node:path');
const { execFile } = require('node:child_process');

function checkParentRoute(input) {
  return new Promise(resolve => {
    const requested=Number(input?.routeTimeoutMs);
    const timeout=Number.isFinite(requested) && requested>0 ? Math.min(12000,Math.max(200,requested)) : 12000;
    execFile('python', [path.join(__dirname, '..', 'scripts', 'check-parent-route.py'),
      String(input?.session_id || ''), String(input?.turn_id || ''), String(input?.routeModel || ''), String(input?.routeRevision || '')],
      {encoding:'utf8', timeout, maxBuffer:4096, windowsHide:true, stdio:['ignore','pipe','pipe']},
      (error, stdout) => {
        if (error) return resolve('unknown');
        try {
          const value = JSON.parse(stdout).route;
          resolve(['compatible','incompatible'].includes(value) ? value : 'unknown');
        } catch { resolve('unknown'); }
      });
  });
}

async function resolveParentRoute(provider, input, check = checkParentRoute) {
  // Some hosts record thread_settings_applied after task_started. In that
  // case the provider field is unknown, but same-turn successful HTTP egress
  // can still prove the actual active route without guessing from config.
  if (provider === 'openai') return 'official';
  if (provider !== 'custom' && provider !== null && provider !== undefined) return 'unknown';
  try { return await check(input); } catch { return 'unknown'; }
}

function currentSession(sessionId) {
  return new Promise(resolve => {
    if (!sessionId) return resolve(null);
    execFile('python', [path.join(__dirname, '..', 'scripts', 'current-session.py'), String(sessionId)],
      {encoding:'utf8', timeout:1200, maxBuffer:4096, windowsHide:true, stdio:['ignore','pipe','pipe']},
      (error, stdout) => {
        if (error) return resolve(null);
        try { resolve(JSON.parse(stdout)); } catch { resolve(null); }
      });
  });
}

function messageTargetModel(context, target) {
  return new Promise(resolve => {
    if (!context || typeof target !== 'string' || !target) return resolve(null);
    execFile('python',[path.join(__dirname,'..','scripts','v2-message-target.py'),context.parentId,context.agentPath,target],
      {encoding:'utf8',timeout:1200,maxBuffer:4096,windowsHide:true,stdio:['ignore','pipe','pipe']},
      (error,stdout)=>{
        if(error)return resolve(null);
        try{resolve(JSON.parse(stdout).model || null);}catch{resolve(null);}
      });
  });
}
module.exports = { checkParentRoute, resolveParentRoute, currentSession, messageTargetModel };
