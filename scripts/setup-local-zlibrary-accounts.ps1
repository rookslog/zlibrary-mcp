[CmdletBinding()]
param(
    [ValidateRange(1, 15)]
    [int]$AdditionalAccounts = 3
)

$ErrorActionPreference = 'Stop'

if ($env:OS -ne 'Windows_NT') {
    throw 'This helper stores credentials with Windows user-bound encryption and runs on Windows only.'
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw 'LOCALAPPDATA is unavailable for this Windows user.'
}

$accountRoot = Join-Path $env:LOCALAPPDATA 'zlibrary-mcp'
$primaryPath = Join-Path $accountRoot 'account.xml'
$extraRoot = Join-Path $accountRoot 'accounts'
if (-not (Test-Path -LiteralPath $primaryPath)) {
    throw 'A primary account.xml is not configured. Configure the primary account first, then add extra accounts.'
}

try {
    $primary = Import-Clixml -LiteralPath $primaryPath
    if ($primary -isnot [System.Management.Automation.PSCredential]) { throw 'invalid' }
} catch {
    throw 'The existing primary credential cannot be read by this Windows user.'
}

New-Item -ItemType Directory -Path $extraRoot -Force | Out-Null
$configuredEmails = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
[void]$configuredEmails.Add($primary.UserName.Trim())
$occupiedSlots = [System.Collections.Generic.HashSet[int]]::new()
foreach ($file in Get-ChildItem -LiteralPath $extraRoot -Filter 'account-*.xml' -File) {
    if ($file.BaseName -notmatch '^account-(\d{2})$') { continue }
    $slot = [int]$Matches[1]
    if ($slot -lt 2 -or $slot -gt 16) { continue }
    try {
        $existing = Import-Clixml -LiteralPath $file.FullName
        if ($existing -isnot [System.Management.Automation.PSCredential]) { throw 'invalid' }
    } catch {
        throw "A saved account slot $slot cannot be read by this Windows user; no new accounts were added."
    }
    [void]$occupiedSlots.Add($slot)
    [void]$configuredEmails.Add($existing.UserName.Trim())
}

$availableSlots = @(2..16 | Where-Object { -not $occupiedSlots.Contains($_) })
if ($availableSlots.Count -lt $AdditionalAccounts) {
    throw "Only $($availableSlots.Count) additional slots remain; no new accounts were added."
}

$savedSlots = [System.Collections.Generic.List[int]]::new()
for ($index = 0; $index -lt $AdditionalAccounts; $index++) {
    $slot = $availableSlots[$index]
    $email = (Read-Host "Z-Library email for account slot $slot").Trim()
    if ([string]::IsNullOrWhiteSpace($email)) { throw "Email for slot $slot is required." }
    if ($configuredEmails.Contains($email)) {
        throw "An account with that email is already configured; no credential was saved for slot $slot."
    }
    $password = Read-Host "Z-Library password for account slot $slot" -AsSecureString
    if ($password.Length -eq 0) { throw "Password for slot $slot is required." }

    $credential = [System.Management.Automation.PSCredential]::new($email, $password)
    $target = Join-Path $extraRoot ('account-{0:D2}.xml' -f $slot)
    $temporary = Join-Path $extraRoot ('.account-{0:D2}-{1}.tmp.xml' -f $slot, [guid]::NewGuid().ToString('N'))
    try {
        $credential | Export-Clixml -LiteralPath $temporary
        [System.IO.File]::Move($temporary, $target)
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Force
        }
    }
    [void]$configuredEmails.Add($email)
    [void]$savedSlots.Add($slot)
}

Write-Host "Saved $($savedSlots.Count) additional account(s) in Windows user-bound encrypted files. Passwords were not displayed."
Write-Host "Account slots added: $($savedSlots -join ', '). Restart the local Z-Library MCP launcher to load them."
