$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& "$PSScriptRoot\.venv\Scripts\python.exe" start.py --fix --no-browser --config config.tt-uat.json --status status.tt-uat.json --commands commands.tt-uat.jsonl --results results.tt-uat.json --port 8000
