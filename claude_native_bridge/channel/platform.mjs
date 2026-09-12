// Filesystem policy only; HTTP/MCP protocol stays platform independent.
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';

function assertNoLinks(target) {
  const stat = fs.lstatSync(target);
  if (stat.isSymbolicLink()) throw new Error('linked runtime path');
  return stat;
}

function windowsPrivatePath(target, directory) {
  // Node stat has no general FILE_ATTRIBUTE_REPARSE_POINT field. Use the
  // built-in Windows ACL API via PowerShell, not POSIX mode/guessed usernames.
  // LiteralPath and a quoted data literal prevent path-to-command injection.
  const literal = target.replaceAll("'", "''");
  const script = `
$ErrorActionPreference = 'Stop'
$p = '${literal}'
$item = Get-Item -LiteralPath $p -Force
$walk = $item
while ($null -ne $walk) {
  if (($walk.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'reparse' }
  if ($walk -is [IO.DirectoryInfo]) { $walk = $walk.Parent } else { $walk = $walk.Directory }
}
if ($item.PSIsContainer -ne $${directory ? 'true' : 'false'}) { throw 'type' }
$acl = Get-Acl -LiteralPath $p
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value) { throw 'owner' }
${directory ? "if (-not $acl.AreAccessRulesProtected) { throw 'inherited directory ACL' }" : ''}
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
if ($rules.Count -eq 0) { throw 'empty ACL' }
foreach ($rule in $rules) {
  if ($rule.IdentityReference.Value -ne $sid.Value -or $rule.AccessControlType -ne 'Allow') { throw 'public ACL' }
}
`;
  // No PATH lookup of an arbitrary powershell executable. Fail closed when the
  // system installation is absent or enterprise policy blocks the ACL check.
  const root = process.env.SystemRoot;
  if (!root || !path.isAbsolute(root)) throw new Error('Windows system directory');
  const powershell = path.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  execFileSync(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand',
    Buffer.from(script, 'utf16le').toString('base64')],
  {stdio: ['ignore', 'ignore', 'pipe'], timeout: 10000, windowsHide: true});
}

export function assertPrivateRuntime(dir) {
  if (!dir || !path.isAbsolute(dir)) throw new Error('runtime');
  const stat = assertNoLinks(dir);
  if (!stat.isDirectory()) throw new Error('runtime');
  if (process.platform === 'win32') {
    windowsPrivatePath(dir, true);
  } else if ((stat.mode & 0o077) || stat.uid !== process.getuid()) {
    throw new Error('runtime');
  }
}

export function readTransport(dir) {
  assertPrivateRuntime(dir);
  const file = path.join(dir, 'transport.json');
  const before = assertNoLinks(file);
  if (!before.isFile() || before.size > 8192 || before.nlink !== 1) throw new Error('transport');
  if (process.platform === 'win32') windowsPrivatePath(file, false);
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.size > 8192 || stat.nlink !== 1 ||
        stat.dev !== before.dev || stat.ino !== before.ino) throw new Error('transport');
    if (process.platform !== 'win32' && ((stat.mode & 0o077) || stat.uid !== process.getuid())) {
      throw new Error('transport');
    }
    return JSON.parse(fs.readFileSync(fd, 'utf8'));
  } finally {
    fs.closeSync(fd);
  }
}
