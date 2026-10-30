// Start the per-user runtime without embedding a user-specific absolute path.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

const local = process.env.LOCALAPPDATA;
const python = local && path.join(local, 'CodexProviderHub', 'runtime', 'Scripts', 'python.exe');
if (!python || !fs.existsSync(python)) {
  process.stderr.write('模型连接中心运行环境尚未安装，请参阅 README。\n');
  process.exit(1);
}

const child = spawn(python, ['-B', path.join(__dirname, '..', 'mcp_server.py')], {
  stdio: 'inherit', windowsHide: true,
});
child.on('error', () => {
  process.stderr.write('无法启动模型连接中心运行环境。\n');
  process.exitCode = 1;
});
child.on('exit', (code, signal) => { process.exitCode = signal ? 1 : (code ?? 1); });
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => child.kill(signal));
}
