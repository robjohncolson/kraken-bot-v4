# One-time setup: registers a Windows Scheduled Task that runs cc_brain.py
# every 2 hours. Idempotent — re-running replaces the existing task.
#
# Run this script ONCE after the first setup. You don't normally invoke it.

$taskName = "KrakenBot-CcBrainCycle"
$wrapperScript = "C:\Users\rober\Downloads\Projects\kraken-bot-v4\scripts\run_cc_brain_scheduled.ps1"

# Action: invoke PowerShell to run our wrapper (hidden window).
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapperScript`""

# Trigger: start 2 minutes from now, then every 2h for 10 years.
$startTime = (Get-Date).AddMinutes(2)
$trigger = New-ScheduledTaskTrigger -Once -At $startTime `
    -RepetitionInterval (New-TimeSpan -Hours 2) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# Settings: start when available (catches up missed runs), don't stop on battery,
# don't stop if PC goes idle, allow retries.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -RunOnlyIfNetworkAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

# Register (or replace) the task under the current user, limited privilege.
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Runs cc_brain.py every 2h. Logs to state/scheduled-logs/." `
    -Force

Write-Host ""
Write-Host "Registered task: $taskName" -ForegroundColor Green
Write-Host "  First run:     $($startTime.ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host "  Interval:      every 2 hours"
Write-Host "  Wrapper:       $wrapperScript"
Write-Host ""
Write-Host "Inspect with:  schtasks /Query /TN $taskName /V /FO LIST"
Write-Host "Trigger now:   schtasks /Run /TN $taskName"
Write-Host "Remove:        schtasks /Delete /TN $taskName /F"
