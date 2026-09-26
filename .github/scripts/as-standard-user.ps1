# Run a command as a fresh standard (non-administrator) user.
#
# Hosted Windows runners are elevated administrators, but the bridge never runs
# elevated on real installs. Files an elevated process creates are owned by the
# Administrators group, which the private-runtime ACL checks rightly refuse, so
# an elevated run tests a configuration users never have.
#
# Windows PowerShell 5.1 only (LocalAccounts module).
param(
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string]$Command
)
$ErrorActionPreference = 'Stop'

function Quote([string]$value) { "'" + $value.Replace("'", "''") + "'" }

$name = 'hcbtest'
$password = 'Aa1!' + [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')
$secure = ConvertTo-SecureString $password -AsPlainText -Force
if (Get-LocalUser -Name $name -ErrorAction SilentlyContinue) {
    Set-LocalUser -Name $name -Password $secure
} else {
    New-LocalUser -Name $name -Password $secure -PasswordNeverExpires -AccountNeverExpires | Out-Null
    Add-LocalGroupMember -Group 'Users' -Member $name
}

$scratch = Join-Path $env:RUNNER_TEMP 'standard-user'
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
foreach ($path in @($env:GITHUB_WORKSPACE, $scratch)) {
    & icacls.exe $path /grant "${name}:(OI)(CI)M" /Q | Out-Null
    if ($LASTEXITCODE) { throw "icacls failed for $path" }
}

$log = Join-Path $scratch 'output.log'
Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue
$lines = @('$ErrorActionPreference = ''Continue''')
foreach ($key in 'PATH', 'HERMES_AGENT_REPO', 'PYTHONUTF8', 'pythonLocation') {
    $value = [Environment]::GetEnvironmentVariable($key)
    if ($value) { $lines += "`$env:$key = $(Quote $value)" }
}
$lines += @(
    "`$log = $(Quote $log)",
    '$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())',
    'if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { ''still an administrator'' | Out-File -LiteralPath $log; exit 99 }',
    '# The launcher''s profile variables are inherited; point them at this user''s profile.',
    '$local = [Environment]::GetFolderPath(''LocalApplicationData'')',
    '$env:USERPROFILE = [Environment]::GetFolderPath(''UserProfile''); $env:HOME = $env:USERPROFILE',
    '$env:APPDATA = [Environment]::GetFolderPath(''ApplicationData''); $env:LOCALAPPDATA = $local',
    '$env:TEMP = Join-Path $local ''Temp''; $env:TMP = $env:TEMP',
    'New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null',
    '$env:HERMES_HOME = Join-Path $env:TEMP ''hermes-home''',
    "Set-Location -LiteralPath $(Quote $WorkingDirectory)",
    "& { $Command } *>&1 | Out-File -LiteralPath `$log -Encoding utf8",
    'exit $LASTEXITCODE'
)
$child = Join-Path $scratch 'run.ps1'
Set-Content -LiteralPath $child -Value $lines -Encoding UTF8

$credential = New-Object Management.Automation.PSCredential($name, $secure)
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$process = Start-Process -FilePath $powershell -Credential $credential -LoadUserProfile `
    -WorkingDirectory $WorkingDirectory -PassThru `
    -ArgumentList '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $child
$null = $process.Handle  # Keeps ExitCode readable after exit.
$process.WaitForExit()
if (Test-Path -LiteralPath $log) { Get-Content -LiteralPath $log }
exit $process.ExitCode
