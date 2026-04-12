# Wrapper invoked by Windows Task Scheduler.
# Runs a single cc_brain cycle and writes stdout/stderr to a timestamped log.
# Exit code propagates so the Task Scheduler history reflects success/failure.

$ErrorActionPreference = "Continue"
$projectRoot = "C:\Users\rober\Downloads\Projects\kraken-bot-v4"
$python = "C:\Python313\python.exe"

$logDir = Join-Path $projectRoot "state\scheduled-logs"
if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
}

# Retain only the most recent 200 scheduled-log files (about ~17 days at 2h cadence).
Get-ChildItem -Path $logDir -Filter "cc_brain_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 200 |
    Remove-Item -Force -ErrorAction SilentlyContinue

$timestamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$logFile = Join-Path $logDir "cc_brain_$timestamp.log"

Set-Location $projectRoot

# Run cc_brain.py and tee output. *>&1 merges stderr into stdout.
& $python "scripts\cc_brain.py" *>&1 | Tee-Object -FilePath $logFile

exit $LASTEXITCODE
