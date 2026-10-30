// Credential transport for the bundled OpenCodex keyring. Secrets are accepted
// only on stdin and are never printed, included in arguments, or persisted here.
const fs = require('node:fs');
const path = require('node:path');

function send(value) { process.stdout.write(JSON.stringify(value)); }
try {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const binding = process.env.CODEX_PROVIDER_HUB_KEYRING;
  if (!binding || !path.isAbsolute(binding) || !['write', 'verify', 'discard'].includes(input.action)
      || typeof input.account !== 'string'
      || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(input.account)) throw new Error();
  const {Entry} = require(binding);
  const entry = new Entry('opencodex.provider-api-key.v1', input.account);
  if (input.action === 'discard') {
    send({ok: entry.getPassword() === null || entry.deletePassword()});
  } else {
    if (typeof input.secret !== 'string' || !input.secret || input.secret.length > 8192 || /[\r\n\0]/.test(input.secret)) throw new Error();
    if (input.action === 'write') {
      if (entry.getPassword() !== null) throw new Error();
      let created = false;
      try {
        entry.setPassword(input.secret); created = true;
        if (entry.getPassword() !== input.secret) throw new Error();
      } catch (error) {
        if (created) entry.deletePassword();
        throw error;
      }
    }
    send({ok: entry.getPassword() === input.secret});
  }
} catch {
  send({ok: false, code: 'credential_store_unavailable'});
  process.exitCode = 1;
}
