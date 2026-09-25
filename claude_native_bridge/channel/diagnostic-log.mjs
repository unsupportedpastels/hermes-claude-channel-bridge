import fs from 'node:fs';
import path from 'node:path';

export const DIAGNOSTIC_MAX_BYTES = 128 * 1024;
export const DIAGNOSTIC_BACKUPS = 2;

// A single channel process owns its private runtime. Do not write to stderr:
// the native CLI persists MCP stderr outside this private directory.
export function appendDiagnostic(dir, event, metadata = {}, maxBytes = DIAGNOSTIC_MAX_BYTES) {
  const line = JSON.stringify({time: new Date().toISOString(), event, ...metadata}) + '\n';
  const bytes = Buffer.byteLength(line, 'utf8');
  if (bytes > maxBytes) return;
  const file = path.join(dir, 'channel-diagnostics.log');
  const current = fs.existsSync(file) ? fs.statSync(file).size : 0;
  if (current + bytes > maxBytes) {
    fs.rmSync(`${file}.${DIAGNOSTIC_BACKUPS}`, {force: true});
    for (let number = DIAGNOSTIC_BACKUPS - 1; number > 0; number--) {
      if (fs.existsSync(`${file}.${number}`)) fs.renameSync(`${file}.${number}`, `${file}.${number + 1}`);
    }
    if (fs.existsSync(file)) fs.renameSync(file, `${file}.1`);
  }
  fs.appendFileSync(file, line, {encoding: 'utf8', mode: 0o600});
}
