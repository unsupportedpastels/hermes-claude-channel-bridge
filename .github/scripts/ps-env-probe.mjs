// Reproduce the channel server's ACL check under the MCP SDK's default child env.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const channel = process.argv[2];
const {windowsPowerShell, assertPrivateRuntime} = await import(pathToFileURL(path.join(channel, 'platform.mjs')).href);
const {getDefaultEnvironment} = await import(pathToFileURL(path.join(channel, 'node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js')).href);

const full = {...process.env};
const sdk = getDefaultEnvironment();
console.log('sdk env keys:', Object.keys(sdk).join(','));

function attempt(label, env, fn) {
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, env);
  const t = Date.now();
  try { fn(); console.log(label, 'OK', Date.now() - t, 'ms'); }
  catch (e) {
    const err = (e.stderr ? e.stderr.toString() : '') || String(e);
    const lines = [...err.matchAll(/<S S="Error">(.*?)<\/S>/g)].map(m => m[1].replaceAll('_x000D__x000A_', ' ').trim());
    console.log(label, 'FAIL', Date.now() - t, 'ms', '|', (lines.length ? lines.join(' ') : err).slice(0, 700));
  } finally {
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, full);
  }
}

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hcb-probe-'));
const literal = dir.replaceAll("'", "''");
windowsPowerShell(`$ErrorActionPreference='Stop'
  $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
  $acl=New-Object Security.AccessControl.DirectorySecurity
  $acl.SetOwner($sid)
  $acl.SetAccessRuleProtection($true,$false)
  $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')))
  Set-Acl -LiteralPath '${literal}' -AclObject $acl`, {stdio: 'pipe'});
fs.writeFileSync(path.join(dir, 'transport.json'), '{"token":"x"}');

attempt('full env / Get-Acl      ', full, () => windowsPowerShell(`$ErrorActionPreference='Stop'; Get-Acl -LiteralPath '${literal}' | Out-Null`, {stdio: 'pipe'}));
attempt('sdk env  / Get-Acl      ', sdk, () => windowsPowerShell(`$ErrorActionPreference='Stop'; Get-Acl -LiteralPath '${literal}' | Out-Null`, {stdio: 'pipe'}));
attempt('full env / private check', full, () => assertPrivateRuntime(dir));
attempt('sdk env  / private check', sdk, () => assertPrivateRuntime(dir));
fs.rmSync(dir, {recursive: true, force: true});
