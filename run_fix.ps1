$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

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

& $python start.py --fix --no-browser --config config.tt-uat.json --status status.tt-uat.json --commands commands.tt-uat.jsonl --results results.tt-uat.json --port 8000
