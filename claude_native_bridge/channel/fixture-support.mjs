import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {randomBytes} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {setTimeout as delay} from 'node:timers/promises';
import {Client} from '@modelcontextprotocol/sdk/client/index.js';
import {StdioClientTransport} from '@modelcontextprotocol/sdk/client/stdio.js';
import {z} from 'zod';
import {windowsPowerShell} from './platform.mjs';

function secureWindowsFixture(dir) {
  if (process.platform !== 'win32') return;
  const literal = dir.replaceAll("'", "''");
  const script = `$ErrorActionPreference='Stop'
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl=New-Object Security.AccessControl.DirectorySecurity
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true,$false)
    $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath '${literal}' -AclObject $acl`;
  windowsPowerShell(script, {stdio: 'pipe'});
}

export async function until(check, timeout = 4000) {
  const start = Date.now();
  for (;;) {
    const result = await check();
    if (result) return result;
    assert.ok(Date.now() - start < timeout, 'Condition did not become true before deadline');
    await delay(10);
  }
}

export async function fixture(t, {diagnostics = false} = {}) {
  const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'hermes-channel-offline-'));
  secureWindowsFixture(dir);
  const token = randomBytes(32).toString('hex');
  await fs.writeFile(path.join(dir, 'transport.json'), JSON.stringify({token}), {mode: 0o600});
  const transport = new StdioClientTransport({
    command: process.execPath, args: [fileURLToPath(new URL('./server.mjs', import.meta.url))],
    env: {HERMES_BRIDGE_RUNTIME_DIR: dir, HERMES_BRIDGE_DIAGNOSTICS: diagnostics ? '1' : '0'}, stderr: 'pipe', maxBufferSize: 9 * 1024 * 1024,
  });
  const client = new Client({name: 'offline-protocol-test', version: '1.0.0'}, {capabilities: {}});
  const channels = [];
  const logs = [];
  transport.stderr.on('data', data => logs.push(data.toString()));
  client.setNotificationHandler(z.object({method: z.literal('notifications/claude/channel'), params: z.any()}), notification => channels.push(notification.params));
  t.after(async () => { await client.close(); await fs.rm(dir, {recursive: true, force: true}); });
  await client.connect(transport);
  const ready = await until(async () => {
    try { return JSON.parse(await fs.readFile(path.join(dir, 'ready.json'), 'utf8')); }
    catch (error) { if (error.code === 'ENOENT') return false; throw error; }
  });
  const api = async (endpoint, body, options = {}) => {
    const response = await fetch(`http://127.0.0.1:${ready.port}${endpoint}`, {
      method: options.method ?? (body === undefined ? 'GET' : 'POST'),
      headers: {Authorization: options.auth === undefined ? `Bearer ${token}` : options.auth, 'Content-Type': 'application/json', ...options.headers},
      body: options.raw ?? (body === undefined ? undefined : JSON.stringify(body)),
      signal: options.signal ?? AbortSignal.timeout(15_000),
    });
    return {status: response.status, body: await response.json()};
  };
  const respond = (arguments_, options = {}) => {
    const promise = client.callTool({name: 'respond', arguments: arguments_}, undefined, {timeout: 20_000, ...options});
    promise.catch(() => {});
    return promise;
  };
  const advance = (ack, request_id, content = 'authoritative task') => api('/advance', {ack, request: {request_id, content}});
  return {client, transport, channels, logs, ready, dir, token, api, respond, advance};
}
