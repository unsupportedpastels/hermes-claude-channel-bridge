import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {fixture, until} from './fixture-support.mjs';

const final = {request_id: 'a', kind: 'final', text: 'held'};
const gone = async pid => until(() => {
  try { process.kill(pid, 0); return false; }
  catch (error) { if (error.code === 'ESRCH') return true; throw error; }
});
const readyRemoved = async f => assert.rejects(fs.stat(path.join(f.dir, 'ready.json')), {code: 'ENOENT'});

test('stdin EOF exits promptly, rejects held SDK call, and removes owned readiness/lock files', {timeout: 10_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'a');
  const pending = f.respond(final);
  const rejected = assert.rejects(pending, /closed/i);
  await f.api('/response?after=0');
  const pid = f.ready.pid;
  const start = Date.now();
  await f.client.close();
  await rejected;
  await gone(pid);
  assert.ok(Date.now() - start < 1500, 'EOF must exit before SDK SIGTERM fallback');
  await readyRemoved(f);
  await assert.rejects(fs.stat(path.join(f.dir, 'bridge.lock')), {code: 'ENOENT'});
  assert.ok((await fs.stat(path.join(f.dir, 'transport.json'))).isFile());
});

test('SIGTERM cleans pending native and HTTP requests without an orphan server', {
  timeout: 10_000,
  // Node implements kill(pid, 'SIGTERM') with TerminateProcess on Windows, so
  // the child cannot run its JS signal handler there. Windows cleanup is covered
  // by the stdin-EOF test above and the native Job Object lifecycle tests.
  skip: process.platform === 'win32' ? 'Windows has no catchable SIGTERM' : false,
}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'a');
  const pending = f.respond(final);
  const rejected = assert.rejects(pending, /closed|terminated/i);
  await f.api('/response?after=0');
  const waiting = f.api('/response?after=1').catch(() => null);
  process.kill(f.ready.pid, 'SIGTERM');
  await rejected;
  const response = await waiting;
  assert.ok(response === null || response.status === 503);
  await gone(f.ready.pid);
  await readyRemoved(f);
});

test('malformed native stdio fails the session and rejects the existing held call', {timeout: 10_000}, async t => {
  const f = await fixture(t);
  await f.advance(null, 'a');
  const pending = f.respond(final);
  const rejected = assert.rejects(pending, /native_transport_error/);
  await f.api('/response?after=0');
  // The SDK transport writes a malformed envelope; no mocked Server/Client is involved.
  await f.transport.send({jsonrpc: '2.0', id: 999, method: 123});
  await rejected;
  assert.equal((await f.api('/status')).body.failed, 'native_transport_error');
  assert.equal((await f.advance(1, 'b')).status, 503);
});

async function rejectedStartup(dir) {
  const child = spawn(process.execPath, [fileURLToPath(new URL('./server.mjs', import.meta.url))], {
    env: {...process.env, HERMES_BRIDGE_RUNTIME_DIR: dir}, stdio: ['pipe', 'pipe', 'pipe'],
  });
  const output = [];
  child.stdout.on('data', chunk => output.push(chunk.toString()));
  child.stderr.on('data', chunk => output.push(chunk.toString()));
  const timer = setTimeout(() => child.kill('SIGKILL'), 3000);
  try {
    const code = await new Promise((resolve, reject) => { child.once('exit', resolve); child.once('error', reject); });
    assert.equal(code, 1);
    assert.ok(output.join('').includes('startup failed'));
  } finally { clearTimeout(timer); }
  return output.join('');
}

test('unsafe runtime permissions and existing readiness are refused without overwriting state', {timeout: 10_000}, async t => {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-channel-startup-'));
  t.after(() => fs.rm(dir, {recursive: true, force: true}));
  const config = JSON.stringify({token: 'offline-startup-test-value-not-a-real-secret'});
  await fs.writeFile(path.join(dir, 'transport.json'), config, {mode: 0o600});
  await fs.chmod(dir, 0o755);
  const output = await rejectedStartup(dir);
  assert.ok(!output.includes('offline-startup-test-value'));
  await fs.chmod(dir, 0o700);
  const sentinel = '{"port":123,"pid":456}';
  await fs.writeFile(path.join(dir, 'ready.json'), sentinel, {mode: 0o600});
  await rejectedStartup(dir);
  assert.equal(await fs.readFile(path.join(dir, 'ready.json'), 'utf8'), sentinel);
  assert.equal(await fs.readFile(path.join(dir, 'transport.json'), 'utf8'), config);
});
