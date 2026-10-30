'use strict';
const path = require('node:path');
const { execFileSync } = require('node:child_process');

function loadDisabledModels(options = {}) {
  try {
    const database = options.databasePath || (process.env.LOCALAPPDATA
      && path.join(process.env.LOCALAPPDATA, 'CodexProviderHub', 'profiles.sqlite'));
    if (!database) throw new Error();
    const result = JSON.parse(execFileSync(options.pythonExecutable || 'python',
      [path.join(__dirname, '..', 'scripts', 'disabled-models.py'), path.resolve(database)],
      { encoding: 'utf8', timeout: 2500, maxBuffer: 1024 * 1024, windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'] }));
    if (!Array.isArray(result.models) || result.models.some(model => typeof model !== 'string' || !model)) throw new Error();
    return new Set(result.models);
  } catch {
    const error = new Error('本机连接停用状态不可读取，已停止调度；请检查本机连接数据库。');
    error.code = 'MODEL_DISABLE_STATE_UNAVAILABLE';
    throw error;
  }
}

function filterDisabledModels(catalog, disabled) {
  return { ...catalog, models: Object.fromEntries(Object.entries(catalog.models)
    .filter(([model]) => !disabled.has(model))) };
}

module.exports = { loadDisabledModels, filterDisabledModels };
