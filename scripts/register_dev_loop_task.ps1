# Register the CC Orchestrator scheduled task in Windows Task Scheduler.
#
# Fires scripts/dev_loop.ps1 every 6 hours, offset from KrakenBot-CcBrainCycle
# (which fires every 2h on the hour) so they don't overlap.
#
# Usage:
#   pwsh -File scripts/register_dev_loop_task.ps1
#   pwsh -File scripts/register_dev_loop_task.ps1 -Unregister

param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$TaskName = "KrakenBot-CcOrchestrator"
$RepoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$ScriptPath = Join-Path $RepoRoot "scripts/dev_loop.ps1"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered $TaskName"
    } else {
        Write-Host "$TaskName not found, nothing to unregister"
    }
    exit 0
}

if (-not (Test-Path $ScriptPath)) {
    Write-Error "dev_loop.ps1 not found at $ScriptPath"
    exit 1
}

# Action: invoke pwsh with the wrapper script
# Use the working dir = RepoRoot so all relative paths in the wrapper resolve correctly
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
    -WorkingDirectory $RepoRoot

# Trigger: every 6 hours starting at a 30-min offset from the brain cycle
# Brain runs at :00 and :30 (every 30 min in --loop mode); let's offset to :15
# First trigger: today at the next :15 boundary that's at least 5 min away
$now = Get-Date
$startTime = (Get-Date).Date.AddHours($now.Hour).AddMinutes(15)
if ($startTime -lt $now.AddMinutes(5)) {
    $startTime = $startTime.AddHours(1)
}
# Round to the next 6h slot from $startTime
# Just use $startTime as-is — task will fire at $startTime, then every 6h after

$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At $startTime `
    -RepetitionInterval (New-TimeSpan -Hours 6)

# Settings: don't run if user is busy, allow battery, etc.
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 0

# Run as current user (interactive token, can read user files)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited

$Task = New-ScheduledTask `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "CC Orchestrator — autonomous dev loop for kraken-bot-v4. Runs every 6h. See CONTINUATION_PROMPT_cc_orchestrator.md."

# Unregister existing if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Unregistering existing $TaskName..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null

Write-Host ""
Write-Host "Registered: $TaskName"
Write-Host "  Action:    $ScriptPath"
Write-Host "  WorkDir:   $RepoRoot"
Write-Host "  First fire: $startTime"
Write-Host "  Interval:  every 6 hours"
Write-Host "  Time limit: 30 minutes"
Write-Host ""
Write-Host "View status:"
Write-Host "  Get-ScheduledTask -TaskName $TaskName"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host ""
Write-Host "Disable temporarily:"
Write-Host "  New-Item state/dev-loop/disabled -ItemType File -Force"
Write-Host ""
Write-Host "Unregister entirely:"
Write-Host "  pwsh -File scripts/register_dev_loop_task.ps1 -Unregister"
