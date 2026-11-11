# Desk 2 paper process. Desk 1 is not started or stopped by this script.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)
$env:DESK_INSTANCE = "desk2"
if (-not $env:DESK_TELEGRAM) { $env:DESK_TELEGRAM = "0" }
$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }
& $python main.py --instance desk2
exit $LASTEXITCODE
