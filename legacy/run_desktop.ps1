param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$DesktopArguments
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (Test-Path -LiteralPath $venvPython) {
    & $venvPython (Join-Path $projectRoot "desktop_app.py") @DesktopArguments
} else {
    py -3.11 (Join-Path $projectRoot "desktop_app.py") @DesktopArguments
}
