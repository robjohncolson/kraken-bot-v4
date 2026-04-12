# CC Orchestrator — Dev Loop Wrapper
#
# Pre-flight: gate checks. Invokes claude with the dev_loop_prompt.md.
# Post-flight: parses output, updates state.json, appends to run log.
#
# Usage:
#   pwsh -File scripts/dev_loop.ps1                # normal run
#   pwsh -File scripts/dev_loop.ps1 -DryRun        # gates + invoke, no commits/restarts (relies on prompt to honor flag)
#   pwsh -File scripts/dev_loop.ps1 -Force         # bypass gates (manual fire only)

param(
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $RepoRoot

$StateDir   = Join-Path $RepoRoot "state/dev-loop"
$RunsDir    = Join-Path $StateDir "runs"
$StateFile  = Join-Path $StateDir "state.json"
$DisableFile= Join-Path $StateDir "disabled"
$EscalFile  = Join-Path $StateDir "escalate.md"
$PromptFile = Join-Path $RepoRoot "scripts/dev_loop_prompt.md"
$OrchDoc    = Join-Path $RepoRoot "CONTINUATION_PROMPT_cc_orchestrator.md"

$Ts = (Get-Date).ToUniversalTime().ToString("yyyyMMdd_HHmmss")
$RunLog = Join-Path $RunsDir "$Ts.log"
$RunSummary = Join-Path $RunsDir "$Ts.summary.md"

# Ensure dirs exist
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
New-Item -ItemType Directory -Force -Path $RunsDir  | Out-Null

function Write-RunLog($msg) {
    $stamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    Add-Content -Path $RunLog -Value "[$stamp] $msg"
    Write-Host "[$stamp] $msg"
}

function Load-State {
    if (Test-Path $StateFile) {
        return Get-Content $StateFile -Raw | ConvertFrom-Json
    }
    return [PSCustomObject]@{
        last_run_ts             = $null
        last_run_action         = "init"
        last_spec_slug          = $null
        last_commit_hash        = $null
        consecutive_failures    = 0
        total_runs              = 0
        total_specs_dispatched  = 0
        cumulative_token_input  = 0
        cumulative_token_output = 0
    }
}

function Save-State($state) {
    $state | ConvertTo-Json -Depth 5 | Set-Content -Path $StateFile -Encoding UTF8
}

function Update-Orch-Doc($entry) {
    # Append a new entry to the chronological log at the bottom of CONTINUATION_PROMPT_cc_orchestrator.md
    if (-not (Test-Path $OrchDoc)) {
        Write-RunLog "WARN: $OrchDoc not found, creating minimal stub"
        Set-Content -Path $OrchDoc -Value "# CC Orchestrator — Continuation Prompt`n`n## Run log`n"
    }
    Add-Content -Path $OrchDoc -Value "`n$entry"
}

function Exit-NoAction($reason, $state) {
    Write-RunLog "GATE: $reason — skipping run"
    $state.last_run_ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $state.last_run_action = "skipped:$reason"
    $state.total_runs += 1
    Save-State $state
    Set-Content -Path $RunSummary -Value "## Run $Ts`n`nSkipped: $reason`n"
    Update-Orch-Doc "- $Ts UTC — **skipped** ($reason)"
    exit 0
}

# ============================================================
# PRE-FLIGHT
# ============================================================

Write-RunLog "=== dev_loop start ==="
$state = Load-State

if ((Test-Path $DisableFile) -and -not $Force) {
    Exit-NoAction "kill switch present (state/dev-loop/disabled)" $state
}

if ($state.consecutive_failures -ge 3 -and -not $Force) {
    Exit-NoAction "3+ consecutive failures (auto-disabled until disable file removed)" $state
}

if (Test-Path $EscalFile) {
    Exit-NoAction "escalation file present (state/dev-loop/escalate.md) — user must resolve" $state
}

# Check bot uptime (need health endpoint)
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:58392/api/health" -TimeoutSec 5
    Write-RunLog "bot uptime: $($health.uptime_seconds)s"
    if ($health.uptime_seconds -lt 3600 -and -not $Force) {
        Exit-NoAction "bot uptime < 1h ($([int]$health.uptime_seconds)s)" $state
    }
} catch {
    Exit-NoAction "bot health endpoint unreachable: $_" $state
}

# Check last commit age (kraken-bot-v4)
$lastCommitTs = [int](git log -1 --format=%ct)
$nowTs = [int](Get-Date -UFormat %s)
$commitAgeMin = [math]::Round(($nowTs - $lastCommitTs) / 60.0, 1)
Write-RunLog "last commit age: ${commitAgeMin}m"
if ($commitAgeMin -lt 30 -and -not $Force) {
    Exit-NoAction "last commit < 30min old (${commitAgeMin}m)" $state
}

# Check unstaged user changes
$dirty = git status --porcelain | Where-Object { $_ -notmatch "^\?\?" }
if ($dirty -and -not $Force) {
    Write-RunLog "unstaged user changes detected:"
    $dirty | ForEach-Object { Write-RunLog "  $_" }
    Exit-NoAction "unstaged user changes present (user is mid-edit)" $state
}

# Check daily token budget (soft cap: 320k input/day across all runs)
$today = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
if ($state.last_run_ts -and $state.last_run_ts.StartsWith($today) -and $state.cumulative_token_input -gt 320000) {
    Exit-NoAction "daily token budget exceeded ($($state.cumulative_token_input) tokens)" $state
}

Write-RunLog "all gates passed — invoking claude"

# ============================================================
# INVOKE CLAUDE
# ============================================================

if (-not (Test-Path $PromptFile)) {
    Write-RunLog "ERROR: prompt file missing at $PromptFile"
    Exit-NoAction "prompt file missing" $state
}

$promptText = Get-Content $PromptFile -Raw
$invocationMode = if ($DryRun) { "DRY RUN — claude will be told to plan but not commit/restart" } else { "LIVE" }
Write-RunLog "mode: $invocationMode"

if ($DryRun) {
    $promptText = $promptText + "`n`n## DRY RUN OVERRIDE`n`nThis is a DRY RUN. After Step 4 (dispatch), do NOT commit, do NOT restart. Just log what you would have done. Set action to 'dry_run' in the YAML output."
}

# Build the claude command. We use --print for headless mode.
# --max-turns bounds runtime. --output-format text keeps parsing simple.
# Token tracking: claude --print emits a final summary line with token use.
$ClaudeCmd = "claude"
$ClaudeArgs = @(
    "--print"
    "--max-turns", "60"
    "--permission-mode", "acceptEdits"
)

# Execute
$claudeStart = Get-Date
try {
    $output = $promptText | & $ClaudeCmd @ClaudeArgs 2>&1
    $exitCode = $LASTEXITCODE
} catch {
    Write-RunLog "ERROR invoking claude: $_"
    $state.consecutive_failures += 1
    $state.last_run_action = "error:invoke_failed"
    $state.last_run_ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $state.total_runs += 1
    Save-State $state
    Set-Content -Path $RunSummary -Value "## Run $Ts`n`nFailed to invoke claude: $_`n"
    Update-Orch-Doc "- $Ts UTC — **error** (claude invoke failed: $_)"
    exit 1
}
$claudeDur = (New-TimeSpan -Start $claudeStart -End (Get-Date)).TotalSeconds

# Save raw claude output to run log
Add-Content -Path $RunLog -Value "`n=== claude output (exit=$exitCode, ${claudeDur}s) ===`n"
Add-Content -Path $RunLog -Value ($output -join "`n")

# ============================================================
# POST-FLIGHT
# ============================================================

# Parse the YAML summary block from the output
$yamlMatch = [regex]::Match(($output -join "`n"), '(?ms)---\s*loop_run_summary:(.*?)---')
$parsedAction = "unknown"
$parsedStatus = "unknown"
$parsedSpecSlug = $null
$parsedCommit = $null
$parsedRestart = "none"

if ($yamlMatch.Success) {
    $yaml = $yamlMatch.Groups[1].Value
    if ($yaml -match 'status:\s*(\S+)')        { $parsedStatus = $matches[1] }
    if ($yaml -match 'action:\s*(\S+)')        { $parsedAction = $matches[1] }
    if ($yaml -match 'spec_slug:\s*(\S+)')     { $parsedSpecSlug = $matches[1] -replace '^null$', '' }
    if ($yaml -match 'commit_hash:\s*(\S+)')   { $parsedCommit = $matches[1] -replace '^null$', '' }
    if ($yaml -match 'restarted:\s*(\S+)')     { $parsedRestart = $matches[1] }
    Write-RunLog "parsed: status=$parsedStatus action=$parsedAction slug=$parsedSpecSlug commit=$parsedCommit restart=$parsedRestart"
} else {
    Write-RunLog "WARN: no YAML summary block found in claude output"
}

# Check for escalation
if ((Test-Path $EscalFile) -or $parsedStatus -eq "escalated") {
    Write-RunLog "ESCALATED — incrementing consecutive_failures"
    $state.consecutive_failures += 1
} else {
    $state.consecutive_failures = 0
}

# Update state
$state.last_run_ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$state.last_run_action = "$parsedStatus`:$parsedAction"
if ($parsedSpecSlug -and $parsedSpecSlug -ne "null") {
    $state.last_spec_slug = $parsedSpecSlug
    $state.total_specs_dispatched += 1
}
if ($parsedCommit -and $parsedCommit -ne "null") {
    $state.last_commit_hash = $parsedCommit
}
$state.total_runs += 1

Save-State $state

# Append to orchestrator continuation prompt
$entryParts = @("- $Ts UTC — **$parsedStatus**")
if ($parsedAction -ne "none" -and $parsedAction -ne "unknown") { $entryParts += "action=$parsedAction" }
if ($parsedSpecSlug) { $entryParts += "spec=$parsedSpecSlug" }
if ($parsedCommit) { $entryParts += "commit=$($parsedCommit.Substring(0, [Math]::Min(7, $parsedCommit.Length)))" }
if ($parsedRestart -ne "none" -and $parsedRestart -ne "unknown") { $entryParts += "restarted=$parsedRestart" }
Update-Orch-Doc ($entryParts -join " ")

# Write the per-run summary
$summaryLines = @(
    "## Run $Ts UTC"
    ""
    "- status: $parsedStatus"
    "- action: $parsedAction"
    "- spec_slug: $parsedSpecSlug"
    "- commit: $parsedCommit"
    "- restarted: $parsedRestart"
    "- duration: ${claudeDur}s"
    "- consecutive_failures: $($state.consecutive_failures)"
    ""
    "See full claude output in $RunLog"
)
Set-Content -Path $RunSummary -Value ($summaryLines -join "`n")

Write-RunLog "=== dev_loop end (status=$parsedStatus, dur=${claudeDur}s) ==="
exit 0
