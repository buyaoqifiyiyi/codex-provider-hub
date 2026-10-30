'use strict';
const path = require('node:path');
const fs = require('node:fs');
const {spawnSync} = require('node:child_process');

function persistence(action, payload, busyCode = 'SCHEDULING_STATE_UNAVAILABLE') {
  const bundled = path.join(process.env.LOCALAPPDATA || '', 'CodexProviderHub', 'runtime', 'Scripts', 'python.exe');
  const executable = fs.existsSync(bundled) ? bundled : 'python';
  const result = spawnSync(executable, ['-I', '-B', path.join(__dirname, '..', 'scripts', 'schedule-state.py')], {
    input: JSON.stringify({action, ...payload}), encoding:'utf8', windowsHide:true,
    timeout:action === 'lookup' ? 2500 : 7000, maxBuffer:256 * 1024
  });
  let response;
  try { response = JSON.parse(result.stdout); } catch { /* Never expose paths, input or helper stderr. */ }
  if (result.error || result.status !== 0 || !response?.ok) {
    const code = response?.code === 'STATE_BUSY' ? busyCode : response?.code || 'STATE_UNAVAILABLE';
    const error = new Error(code); error.code = code; throw error;
  }
  return response.value;
}
module.exports = {persistence};
