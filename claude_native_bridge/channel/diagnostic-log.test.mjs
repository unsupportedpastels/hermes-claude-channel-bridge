import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {appendDiagnostic} from './diagnostic-log.mjs';

test('private channel diagnostics rotate without retaining content or growing indefinitely', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-log-'));
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  for (let index = 0; index < 100; index++) appendDiagnostic(dir, 'proposal_rejected', {
    sequence: index, reason: 'tool_absent', tool: 'patch',
  }, 300);
  const files = fs.readdirSync(dir).filter(name => name.startsWith('channel-diagnostics.log'));
  assert.deepEqual(files.sort(), ['channel-diagnostics.log', 'channel-diagnostics.log.1', 'channel-diagnostics.log.2']);
  assert.ok(files.every(name => fs.statSync(path.join(dir, name)).size <= 300));
  const lines = files.flatMap(name => fs.readFileSync(path.join(dir, name), 'utf8').trim().split('\n').map(JSON.parse));
  assert.equal(lines.at(0).event, 'proposal_rejected');
  assert.ok(lines.some(line => line.sequence === 99));
});
