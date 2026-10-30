'use strict';

const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');

function loadAvailableModels(options = {}) {
  const home = options.codexHome || process.env.CODEX_HOME || path.join(os.homedir(), '.codex');
  try {
    // Use the installed Python standard TOML parser, rather than partially
    // parsing a user configuration or starting a network-backed Codex process.
    return JSON.parse(execFileSync(options.pythonExecutable || 'python',
      [path.join(__dirname, '..', 'scripts', 'read-model-catalog.py'), path.resolve(home)],
      { encoding: 'utf8', timeout: 1800, maxBuffer: 1024 * 1024, windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'] }));
  } catch {
    const error = new Error('当前模型目录不可读取，拒绝创建以免使用已移除的模型。');
    error.code = 'MODEL_CATALOG_UNAVAILABLE';
    throw error;
  }
}

module.exports = { loadAvailableModels };
