# Register the CC Orchestrator weekly scheduled task in Windows Task Scheduler.
#
# Fires scripts/dev_loop.ps1 once per week on Sundays at 10:00 local time
# using the weekly review prompt.
#
# Usage:
#   pwsh -File scripts/register_dev_loop_weekly_task.ps1
#   pwsh -File scripts/register_dev_loop_weekly_task.ps1 -Unregister

param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$TaskName = "KrakenBot-CcOrchestrator-Weekly"
$RepoRoot = (Resolve-Path "$PSScriptRoot/..").Path
$ScriptPath = Join-Path $RepoRoot "scripts/dev_loop.ps1"
$PromptPath = Join-Path $RepoRoot "scripts/dev_loop_weekly_prompt.md"
$PromptArgument = "scripts/dev_loop_weekly_prompt.md"

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

if (-not (Test-Path $PromptPath)) {
    Write-Error "dev_loop_weekly_prompt.md not found at $PromptPath"
    exit 1
}

# Action: invoke powershell.exe with the wrapper script and weekly prompt
# Use the working dir = RepoRoot so all relative paths in the wrapper resolve correctly
$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -PromptFile $PromptArgument" `
    -WorkingDirectory $RepoRoot

# Trigger: once per week on Sunday at 10:00 local time
$Trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek Sunday `
    -At 10am

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
    -Description "CC Orchestrator -- autonomous weekly review for kraken-bot-v4. Runs Sundays at 10:00 local time. See CONTINUATION_PROMPT_cc_orchestrator.md."

# Unregister existing if present
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Unregistering existing $TaskName..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -InputObject $Task | Out-Null

Write-Host ""
Write-Host "Registered: $TaskName"
Write-Host "  Action:    $ScriptPath"
Write-Host "  Prompt:    $PromptPath"
Write-Host "  WorkDir:   $RepoRoot"
Write-Host "  Schedule:  Sundays at 10:00 local time"
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
Write-Host "  pwsh -File scripts/register_dev_loop_weekly_task.ps1 -Unregister"
