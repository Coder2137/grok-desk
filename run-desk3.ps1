# Desk 3 paper process. Desk 1 and desk 2 are not started or stopped by this script.
# Secrets are read from the same file as run-desk.ps1 and are never printed.
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath (Split-Path -Parent $MyInvocation.MyCommand.Path)

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { $python = "python" }

$secrets = Join-Path $env:USERPROFILE ".desk-secrets\env"
if (Test-Path -LiteralPath $secrets) {
  $tmp = Join-Path $env:TEMP ("desk3-env-" + [guid]::NewGuid().ToString("n") + ".json")
  & $python -c "import sys; from secrets_env import write_env_json; write_env_json(sys.argv[1], sys.argv[2])" $secrets $tmp
  if ($LASTEXITCODE -ne 0) {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
    Write-Error "Could not read the secrets env file."
    exit 1
  }
  try {
    $map = Get-Content -LiteralPath $tmp -Raw -Encoding utf8 | ConvertFrom-Json
    foreach ($prop in $map.PSObject.Properties) {
      $name = [string]$prop.Name
      if (-not $name) { continue }
      $current = [Environment]::GetEnvironmentVariable($name, "Process")
      if ([string]::IsNullOrEmpty($current)) {
        try {
          Set-Item -Path ("Env:" + $name) -Value ([string]$prop.Value)
        } catch {
          Write-Error "Could not apply a secrets key."
          exit 1
        }
      }
    }
  } finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
  }
}

$force = [Environment]::GetEnvironmentVariable("FORCE_LIVE", "Process")
if (-not [string]::IsNullOrWhiteSpace($force)) {
  $lower = $force.Trim().ToLowerInvariant()
  if ($lower -notin @("0", "false", "no", "off")) {
    Write-Error "FORCE_LIVE is refused."
    exit 1
  }
}

$env:DESK_INSTANCE = "desk3"
$env:DESK_TELEGRAM = "0"

& $python main.py --instance desk3
exit $LASTEXITCODE
