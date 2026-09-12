import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {assertPrivateRuntime, readTransport} from './platform.mjs';
import {execFileSync} from 'node:child_process';

function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hcb-platform-'));
  fs.chmodSync(dir, 0o700);
  t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
  return dir;
}
const unix = {skip: process.platform === 'win32'};
test('private owned directory and transport accepted', unix, t => {
  const dir = fixture(t);
  fs.writeFileSync(path.join(dir, 'transport.json'), '{"token":"abc"}', {mode: 0o600});
  assertPrivateRuntime(dir);
  assert.deepEqual(readTransport(dir), {token: 'abc'});
});
test('public directory rejected', unix, t => {
  const dir = fixture(t);
  fs.chmodSync(dir, 0o755);
  assert.throws(() => assertPrivateRuntime(dir));
});
test('symlink directory and transport rejected', unix, t => {
  const dir = fixture(t);
  const link = path.join(dir, 'link');
  fs.symlinkSync(dir, link);
  assert.throws(() => assertPrivateRuntime(link));
  fs.writeFileSync(path.join(dir, 'source'), '{}', {mode: 0o600});
  fs.symlinkSync(path.join(dir, 'source'), path.join(dir, 'transport.json'));
  assert.throws(() => readTransport(dir));
});
test('public, oversized, and hardlinked transport rejected', unix, t => {
  const dir = fixture(t), file = path.join(dir, 'transport.json');
  fs.writeFileSync(file, '{}', {mode: 0o644});
  assert.throws(() => readTransport(dir));
  fs.chmodSync(file, 0o600);
  fs.writeFileSync(file, 'x'.repeat(8193));
  assert.throws(() => readTransport(dir));
  fs.writeFileSync(file, '{}');
  fs.linkSync(file, path.join(dir, 'alias'));
  assert.throws(() => readTransport(dir));
});
test('relative directory rejected', () => assert.throws(() => assertPrivateRuntime('relative')));

test('real Windows protected ACL accepted; junction and public ACL refused',
  {skip: process.platform !== 'win32'}, t => {
    const dir = fixture(t);
    const ps = path.join(process.env.SystemRoot, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
    const run = script => execFileSync(ps, ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
      Buffer.from(script, 'utf16le').toString('base64')], {timeout: 10000, stdio: 'pipe'});
    const literal = dir.replaceAll("'", "''");
    run(`$ErrorActionPreference='Stop'
      $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
      $acl=New-Object Security.AccessControl.DirectorySecurity
      $acl.SetOwner($sid)
      $acl.SetAccessRuleProtection($true,$false)
      $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
      $acl.AddAccessRule($rule)
      Set-Acl -LiteralPath '${literal}' -AclObject $acl`);
    fs.writeFileSync(path.join(dir, 'transport.json'), '{"token":"windows-test"}');
    assertPrivateRuntime(dir);
    assert.deepEqual(readTransport(dir), {token: 'windows-test'});
    const junction = path.join(dir, 'junction');
    fs.symlinkSync(dir, junction, 'junction');
    assert.throws(() => assertPrivateRuntime(junction));
    fs.unlinkSync(junction);
    run(`$ErrorActionPreference='Stop'
      $acl=Get-Acl -LiteralPath '${literal}'
      $sid=New-Object Security.Principal.SecurityIdentifier('S-1-1-0')
      $rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'ReadAndExecute','Allow')
      $acl.AddAccessRule($rule)
      Set-Acl -LiteralPath '${literal}' -AclObject $acl`);
    assert.throws(() => assertPrivateRuntime(dir));
  });
