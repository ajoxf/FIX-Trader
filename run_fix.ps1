$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$logDirectory = Join-Path $PSScriptRoot 'logs'
$runtimeDirectory = Join-Path $PSScriptRoot 'runtime'
New-Item -ItemType Directory -Force -Path $logDirectory, $runtimeDirectory | Out-Null
$sessionLog = Join-Path $logDirectory ('fixtrader-{0}.log' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

$pythonCandidates = @(
    (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'),
    (Join-Path (Split-Path $PSScriptRoot -Parent) '.venv\Scripts\python.exe')
)
$python = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $python) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $python = $pythonCommand.Source
    } else {
        throw 'Python was not found. Create .venv\Scripts\python.exe and install requirements.txt.'
    }
}

$env:PYTHONUNBUFFERED = '1'
& $python start.py --fix --no-browser --config config.tt-uat.json `
    --status (Join-Path $runtimeDirectory 'status.tt-uat.json') `
    --commands (Join-Path $runtimeDirectory 'commands.tt-uat.jsonl') `
    --results (Join-Path $runtimeDirectory 'results.tt-uat.json') `
    --port 8000 2>&1 | Tee-Object -FilePath $sessionLog
