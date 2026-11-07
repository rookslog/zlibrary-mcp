import { execFileSync, spawn } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const env = { ...process.env };
if (process.platform === 'win32') {
  const script = `
$root = Join-Path $env:LOCALAPPDATA 'zlibrary-mcp'
$entries = @()
$primary = Join-Path $root 'account.xml'
if (Test-Path -LiteralPath $primary) { $entries += @{ slot=1; path=$primary } }
$extras = Join-Path $root 'accounts'
if (Test-Path -LiteralPath $extras) {
  foreach ($file in Get-ChildItem -LiteralPath $extras -Filter 'account-*.xml' -File) {
    if ($file.BaseName -match '^account-(\\d{2})$') { $entries += @{ slot=[int]$Matches[1]; path=$file.FullName } }
  }
}
$accounts = @()
foreach ($entry in ($entries | Sort-Object slot)) {
  $c = Import-Clixml -LiteralPath $entry.path
  if ($c -isnot [System.Management.Automation.PSCredential]) { throw 'Invalid credential file' }
  $accounts += @{ slot=$entry.slot; email=$c.UserName; password=$c.GetNetworkCredential().Password }
}
ConvertTo-Json -InputObject $accounts -Compress
`;
  try {
    const accounts = JSON.parse(
      execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      }),
    );
    if (accounts.length) {
      for (const key of Object.keys(env))
        if (/^ZLIBRARY_(EMAIL|PASSWORD)(?:_\d+)?$/i.test(key)) delete env[key];
      for (const account of accounts) {
        const suffix = account.slot === 1 ? '' : `_${account.slot}`;
        env[`ZLIBRARY_EMAIL${suffix}`] = account.email;
        env[`ZLIBRARY_PASSWORD${suffix}`] = account.password;
      }
    }
  } catch {
    process.stderr.write('Unable to read local Z-Library credentials for this Windows user.\n');
    process.exit(1);
  }
}
env.ZLIBRARY_OUTPUT_ROOT ||= join(root, 'downloads');
const child = spawn(process.execPath, [join(root, 'dist', 'index.js')], {
  env,
  stdio: 'inherit',
  windowsHide: true,
});
child.on('error', () => {
  process.stderr.write('Unable to start the Z-Library MCP server.\n');
  process.exitCode = 1;
});
child.on('exit', (code) => {
  process.exitCode = code ?? 1;
});
