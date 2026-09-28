# Run this script from an Administrator PowerShell. It adds only the two
# outbound ports used by the configured TT UAT FIX sessions.
$ErrorActionPreference = 'Stop'

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator rights are required. Open PowerShell as Administrator and run this script again.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonCandidates = @(
    (Join-Path $projectRoot '.venv\Scripts\python.exe'),
    (Join-Path (Split-Path $projectRoot -Parent) '.venv\Scripts\python.exe')
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    throw 'Project Python was not found. Create .venv\Scripts\python.exe first.'
}

$ruleName = 'TT FIX UAT - FIX-DEV Python'
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $ruleName -Direction Outbound -Action Allow `
    -Program $python -Protocol TCP -RemotePort 11502,11503 -Profile Any | Out-Null
Write-Host "Created outbound firewall rule '$ruleName' for $python" -ForegroundColor Green
Write-Host 'If test_tt_connectivity.ps1 still reports 10013, an antivirus, VPN, or managed network policy is blocking it.' -ForegroundColor Yellow
