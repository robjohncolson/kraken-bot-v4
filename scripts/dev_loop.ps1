# CC Orchestrator -- Dev Loop Wrapper
#
# Pre-flight: gate checks. Invokes claude with the dev_loop_prompt.md.
# Post-flight: parses output, updates state.json, appends to run log.
#
# Usage:
#   pwsh -File scripts/dev_loop.ps1                # normal run
#   pwsh -File scripts/dev_loop.ps1 -DryRun        # observe + diagnose only, no writes
#   pwsh -File scripts/dev_loop.ps1 -Force         # bypass gates (manual fire only)
#   pwsh -File scripts/dev_loop.ps1 -SkipChallenge # skip Codex challenge on no_action

param(
    [switch]$DryRun,
    [switch]$Force,
    [switch]$SkipChallenge
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $RepoRoot

# Force UTF-8 on console output so unicode chars from claude survive the pipe
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

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
        $state = Get-Content $StateFile -Raw | ConvertFrom-Json
        if (-not $state.PSObject.Properties["cumulative_token_input"]) {
            $state | Add-Member -NotePropertyName "cumulative_token_input" -NotePropertyValue 0
        } elseif ($null -eq $state.cumulative_token_input) {
            $state.cumulative_token_input = 0
        }
        if (-not $state.PSObject.Properties["cumulative_token_output"]) {
            $state | Add-Member -NotePropertyName "cumulative_token_output" -NotePropertyValue 0
        } elseif ($null -eq $state.cumulative_token_output) {
            $state.cumulative_token_output = 0
        }
        return $state
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

function Get-JsonPropertyValue($node, [string[]]$propertyNames) {
    if ($null -eq $node) {
        return $null
    }

    $queue = [System.Collections.Queue]::new()
    $queue.Enqueue($node)

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if ($null -eq $current) {
            continue
        }

        if ($current -is [string] -or $current -is [ValueType]) {
            continue
        }

        if ($current -is [System.Collections.IEnumerable] -and -not ($current -is [string])) {
            foreach ($item in $current) {
                $queue.Enqueue($item)
            }
            continue
        }

        foreach ($prop in $current.PSObject.Properties) {
            if ($propertyNames -contains $prop.Name) {
                return $prop.Value
            }
        }

        foreach ($prop in $current.PSObject.Properties) {
            if ($null -ne $prop.Value) {
                $queue.Enqueue($prop.Value)
            }
        }
    }

    return $null
}

function Convert-JsonNodeToText($node) {
    if ($null -eq $node) {
        return $null
    }

    if ($node -is [string]) {
        return $node.Trim()
    }

    if ($node -is [ValueType]) {
        return $null
    }

    if ($node -is [System.Collections.IEnumerable] -and -not ($node -is [string])) {
        $parts = @()
        foreach ($item in $node) {
            $text = Convert-JsonNodeToText $item
            if ($text) {
                $parts += $text
            }
        }
        if ($parts.Count -gt 0) {
            return ($parts -join "`n").Trim()
        }
        return $null
    }

    foreach ($preferredName in @("text", "result", "response", "output", "completion", "message", "content")) {
        $prop = $node.PSObject.Properties[$preferredName]
        if ($prop) {
            $text = Convert-JsonNodeToText $prop.Value
            if ($text) {
                return $text
            }
        }
    }

    foreach ($prop in $node.PSObject.Properties) {
        $text = Convert-JsonNodeToText $prop.Value
        if ($text) {
            return $text
        }
    }

    return $null
}

function Get-ClaudeResponseText($payload) {
    foreach ($propertyName in @("result", "response", "output", "completion", "message", "content")) {
        $value = Get-JsonPropertyValue $payload @($propertyName)
        $text = Convert-JsonNodeToText $value
        if ($text) {
            return $text
        }
    }
    return $null
}

function Get-ClaudeTokenCount($payload, [string[]]$propertyNames) {
    $value = Get-JsonPropertyValue $payload $propertyNames
    if ($null -eq $value) {
        return [long]0
    }

    try {
        return [long]$value
    } catch {
        return [long]0
    }
}

function Update-Orch-Doc($entry) {
    # Append a new entry to the chronological log at the bottom of CONTINUATION_PROMPT_cc_orchestrator.md
    if (-not (Test-Path $OrchDoc)) {
        Write-RunLog "WARN: $OrchDoc not found, creating minimal stub"
        Set-Content -Path $OrchDoc -Value "# CC Orchestrator -- Continuation Prompt`n`n## Run log`n"
    }
    Add-Content -Path $OrchDoc -Value "`n$entry"
}

function Exit-NoAction($reason, $state) {
    Write-RunLog "GATE: $reason -- skipping run"
    $state.last_run_ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $state.last_run_action = "skipped:$reason"
    $state.total_runs += 1
    Save-State $state
    Set-Content -Path $RunSummary -Value "## Run $Ts`n`nSkipped: $reason`n"
    Update-Orch-Doc "- $Ts UTC -- **skipped** ($reason)"
    exit 0
}

function Test-PreviousSpecSettled($state) {
    # The previous spec is "settled" when:
    #   1. At least 1 brain cycle has run AFTER the last commit
    #   2. No new permission_blocked / stuck_dust / reconciliation_anomaly memories
    #      with timestamp > last commit timestamp
    # Returns @{ settled = $true/$false; reason = "..." }

    # If we've never dispatched a spec, nothing to settle
    if (-not $state.last_spec_slug) {
        return @{ settled = $true; reason = "no prior spec" }
    }

    $lastCommitTs = [int](git log -1 --format=%ct)
    $epoch = Get-Date "1970-01-01Z"
    $lastCommitDate = $epoch.AddSeconds($lastCommitTs).ToUniversalTime()
    $lastCommitIso = $lastCommitDate.ToString("yyyy-MM-ddTHH:mm:ssZ")

    # 1. Brain cycles after commit
    $reviewDir = Join-Path $RepoRoot "state/cc-reviews"
    if (-not (Test-Path $reviewDir)) {
        return @{ settled = $false; reason = "state/cc-reviews missing" }
    }
    $cyclesAfter = Get-ChildItem -Path $reviewDir -Filter "brain_*.md" |
        Where-Object { $_.LastWriteTimeUtc -gt $lastCommitDate } |
        Measure-Object | Select-Object -ExpandProperty Count

    if ($cyclesAfter -lt 1) {
        return @{ settled = $false; reason = "no brain cycle after last commit (commit_ts=$lastCommitIso)" }
    }

    # 2. Check for new pathology memories via sqlite (use a temp script file to avoid here-string headaches)
    $dbPath = Join-Path $RepoRoot "data/bot.db"
    if (-not (Test-Path $dbPath)) {
        return @{ settled = $true; reason = "no db, $cyclesAfter brain cycle(s) since commit" }
    }

    $tmpPy = Join-Path $env:TEMP "dev_loop_settled_check.py"
    $pyCode = "import sqlite3, sys`n" +
              "conn = sqlite3.connect(sys.argv[1])`n" +
              "cur = conn.cursor()`n" +
              "cur.execute(`"SELECT COUNT(*) FROM cc_memory WHERE category IN ('permission_blocked','stuck_dust','reconciliation_anomaly') AND timestamp > ?`", (sys.argv[2],))`n" +
              "print(cur.fetchone()[0])`n"
    Set-Content -Path $tmpPy -Value $pyCode -Encoding UTF8

    try {
        $newProblems = & "C:/Python313/python.exe" $tmpPy $dbPath $lastCommitIso 2>&1
        $newProblems = [int]$newProblems
    } catch {
        Write-RunLog "WARN: sqlite query failed: $_ -- assuming settled"
        return @{ settled = $true; reason = "sqlite query failed (treating as settled)" }
    }

    if ($newProblems -gt 0) {
        return @{ settled = $false; reason = "$newProblems new pathology memories since commit" }
    }

    return @{ settled = $true; reason = "$cyclesAfter brain cycle(s) since commit, 0 new pathology" }
}

# ============================================================
# PRE-FLIGHT
# ============================================================

Write-RunLog "=== dev_loop start ==="
$state = Load-State
$todayUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")

if ($state.last_run_ts -and -not $state.last_run_ts.StartsWith($todayUtc)) {
    Write-RunLog "UTC day rollover detected (last_run_ts=$($state.last_run_ts)) -- resetting cumulative token counters"
    $state.cumulative_token_input = 0
    $state.cumulative_token_output = 0
}

if ((Test-Path $DisableFile) -and -not $Force) {
    Exit-NoAction "kill switch present (state/dev-loop/disabled)" $state
}

if ($state.consecutive_failures -ge 3 -and -not $Force) {
    Exit-NoAction "3+ consecutive failures (auto-disabled until disable file removed)" $state
}

if (Test-Path $EscalFile) {
    Exit-NoAction "escalation file present (state/dev-loop/escalate.md) -- user must resolve" $state
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
Write-RunLog "last commit age: $($commitAgeMin)m"
if ($commitAgeMin -lt 30 -and -not $Force) {
    Exit-NoAction "last commit < 30min old ($($commitAgeMin)m)" $state
}

# Check unstaged user changes
$dirty = git status --porcelain | Where-Object { $_ -notmatch "^\?\?" }
if ($dirty -and -not $Force) {
    Write-RunLog "unstaged user changes detected:"
    $dirty | ForEach-Object { Write-RunLog "  $_" }
    Exit-NoAction "unstaged user changes present (user is mid-edit)" $state
}

# Check daily token budget (soft cap: 320k input/day across all runs)
if ($state.last_run_ts -and $state.last_run_ts.StartsWith($todayUtc) -and $state.cumulative_token_input -gt 320000) {
    Exit-NoAction "daily token budget exceeded ($($state.cumulative_token_input) tokens)" $state
}

# Check previous spec is settled (deterministic gate, not LLM judgment)
$settled = Test-PreviousSpecSettled $state
Write-RunLog "previous spec settled: $($settled.settled) ($($settled.reason))"
if (-not $settled.settled -and -not $Force) {
    Exit-NoAction "previous spec unsettled: $($settled.reason)" $state
}

Write-RunLog "all gates passed -- invoking claude"

$lastCodeCommitRaw = @(git log -1 --format=%ct -- '*.py' 2>$null) | Select-Object -First 1
if (-not $lastCodeCommitRaw) {
    $lastCodeCommitRaw = @(git log -1 --format=%ct -- "*.py" 2>$null) | Select-Object -First 1
}
if (-not $lastCodeCommitRaw) {
    throw "unable to determine last .py commit timestamp from git log"
}

$lastCodeCommitTs = [int]("$lastCodeCommitRaw".Trim())
$epoch = Get-Date "1970-01-01Z"
$lastCodeCommitIso = $epoch.AddSeconds($lastCodeCommitTs).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
Write-RunLog "injecting last_code_commit_ts=$lastCodeCommitIso"

# ============================================================
# INVOKE CLAUDE
# ============================================================

if (-not (Test-Path $PromptFile)) {
    Write-RunLog "ERROR: prompt file missing at $PromptFile"
    Exit-NoAction "prompt file missing" $state
}

$promptText = Get-Content $PromptFile -Raw
$runtimeContextBlock = "## RUNTIME CONTEXT (injected by wrapper)`n" +
                       "- last_code_commit_ts: $lastCodeCommitIso`n" +
                       "- Brain reports / memories with timestamps BEFORE this point reflect the OLD code and may show pathology that has already been fixed. When counting ""recurring patterns"" for priority rules 2/4/5/6/7, ONLY count occurrences with timestamp > last_code_commit_ts.`n`n"
$promptText = $runtimeContextBlock + $promptText
$invocationMode = if ($DryRun) { "DRY RUN -- claude will plan but not write/dispatch/commit/restart" } else { "LIVE" }
Write-RunLog "mode: $invocationMode"

if ($DryRun) {
    $dryOverride = "`n`n## DRY RUN OVERRIDE -- STRICT`n`n" +
                   "You are in DRY RUN mode. DO NOT do any of the following:`n" +
                   "- DO NOT write any files to tasks/specs/ (no spec, no plan, no result)`n" +
                   "- DO NOT invoke cross-agent.py (no Codex dispatch)`n" +
                   "- DO NOT run pytest (no verification)`n" +
                   "- DO NOT commit anything (no git add, no git commit)`n" +
                   "- DO NOT restart any process (no taskkill, no main.py launch, no cc_brain launch)`n`n" +
                   "DO the following:`n" +
                   "- Step 1: Observe (read brain reports, query SQLite, check git log)`n" +
                   "- Step 2: Diagnose (pick the single highest-leverage issue from the priority list)`n" +
                   "- Step 3: Decide (state which spec slug you would dispatch and to which repo)`n`n" +
                   "End your response with the YAML summary block, but with status='dry_run' and action='would_dispatch' or 'no_action'."
    $promptText = $promptText + $dryOverride
}

# Build the claude command. We use --print for headless mode.
# --max-turns bounds runtime. bypassPermissions is safe here because the
# wrapper's pre-flight gates + the prompt's hard rules enforce all safety
# constraints (no push, no env edits, no restart if uptime <1h, etc.).
$ClaudeCmd = "claude"
$ClaudeArgs = @(
    "--print"
    "--output-format", "json"
    "--max-turns", "60"
    "--permission-mode", "bypassPermissions"
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
    Update-Orch-Doc "- $Ts UTC -- **error** (claude invoke failed: $_)"
    exit 1
}
$claudeDur = [math]::Round((New-TimeSpan -Start $claudeStart -End (Get-Date)).TotalSeconds, 1)

$rawClaudeOutput = ($output | ForEach-Object { "$_" }) -join "`n"
$claudePayload = $null
$claudeResponseText = $rawClaudeOutput
$parsedInputTokens = [long]0
$parsedOutputTokens = [long]0

try {
    $claudePayload = $rawClaudeOutput | ConvertFrom-Json
    $extractedText = Get-ClaudeResponseText $claudePayload
    if ($extractedText) {
        $claudeResponseText = $extractedText
    } else {
        Write-RunLog "WARN: claude JSON parsed, but no response text field was found; using raw JSON for summary parsing"
    }
    # Total input includes all 3 categories: un-cached input, cache creation, cache read.
    # The 320k/day budget gate counts the FULL input footprint, not just non-cached tokens.
    $uncachedInput = Get-ClaudeTokenCount $claudePayload @("input_tokens", "inputTokens", "prompt_tokens", "promptTokens")
    $cacheCreate   = Get-ClaudeTokenCount $claudePayload @("cache_creation_input_tokens", "cacheCreationInputTokens")
    $cacheRead     = Get-ClaudeTokenCount $claudePayload @("cache_read_input_tokens", "cacheReadInputTokens")
    $parsedInputTokens = [long]($uncachedInput + $cacheCreate + $cacheRead)
    $parsedOutputTokens = Get-ClaudeTokenCount $claudePayload @("output_tokens", "outputTokens", "completion_tokens", "completionTokens")
} catch {
    Write-RunLog "WARN: failed to parse claude JSON output: $_"
}

Add-Content -Path $RunLog -Value "`n=== claude response text (exit=$exitCode, dur=$($claudeDur)s) ===`n"
Add-Content -Path $RunLog -Value $claudeResponseText
Add-Content -Path $RunLog -Value "`n=== claude json metadata ===`n"
Add-Content -Path $RunLog -Value $rawClaudeOutput

# ============================================================
# POST-FLIGHT
# ============================================================

# Parse the YAML summary block from the narrative response text
$yamlMatch = [regex]::Match($claudeResponseText, '(?ms)---\s*loop_run_summary:(.*?)---')
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

$orchDocEntryOverride = $null
$challengeEscalated = $false

# CHALLENGE no_action verdicts
if ($parsedStatus -eq "no_action" -and -not $SkipChallenge -and -not $DryRun) {
    $challengeKeywordPattern = '(?i)(benign|deferred|below threshold|cosmetic|no action needed|matches held)'
    $challengeFinding = $null

    $matchingLines = @(
        $claudeResponseText -split "\r?\n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and $_ -match $challengeKeywordPattern }
    )
    if ($matchingLines.Count -gt 0) {
        $challengeFinding = $matchingLines[0]
    } else {
        $sentenceMatches = [regex]::Matches($claudeResponseText, '(?ms)[^.!?\r\n]+(?:[.!?]|$)')
        foreach ($sentenceMatch in $sentenceMatches) {
            $candidate = $sentenceMatch.Value.Trim()
            if ($candidate -and $candidate -match $challengeKeywordPattern) {
                $challengeFinding = $candidate
                break
            }
        }
    }

    if (-not $challengeFinding) {
        Write-RunLog "no challenge target found"
    } else {
        $challengeOwnedPath = "state/dev-loop/challenge-$Ts.md"
        $challengeFile = Join-Path $RepoRoot $challengeOwnedPath
        $challengePrompt = @"
You are challenging the CC orchestrator's no_action verdict. The orchestrator observed kraken-bot-v4 state and concluded no action is warranted.

ORCHESTRATOR VERDICT (verbatim):
$claudeResponseText

SPECIFIC FINDING TO VERIFY:
$challengeFinding

YOUR TASK:
Read the relevant code (runtime_loop.py, web/routes.py, scripts/cc_brain.py, persistence/sqlite.py) and query data/bot.db directly to verify or refute the verdict. Do not trust the orchestrator's narrative. Look at the actual data.

Write your conclusion to $challengeOwnedPath with this structure:
---
verdict: agree | disagree
evidence:
- <specific facts you observed>
recommended_action: <only if disagree - what spec should be dispatched>
---

Be terse. Do not write a long essay. The point is fast independent verification.
"@

        $challengeFailureReason = $null
        try {
            $challengeOutput = & "C:/Python313/python.exe" "/c/Users/rober/Downloads/Projects/Agent/runner/cross-agent.py" `
                "--direction" "cc-to-codex" `
                "--task-type" "investigate" `
                "--working-dir" $RepoRoot `
                "--owned-paths" $challengeOwnedPath `
                "--timeout" "600" `
                "--prompt" $challengePrompt 2>&1
            $challengeExitCode = $LASTEXITCODE
        } catch {
            $challengeExitCode = 1
            $challengeOutput = @($_.Exception.Message)
        }

        if ($challengeExitCode -ne 0) {
            $challengeOutputText = (($challengeOutput | ForEach-Object { "$_" }) -join " ").Trim()
            if ($challengeOutputText.Length -gt 240) {
                $challengeOutputText = $challengeOutputText.Substring(0, 240) + "..."
            }
            $challengeFailureReason = "exit=$challengeExitCode"
            if ($challengeOutputText) {
                $challengeFailureReason += " $challengeOutputText"
            }
        } elseif (-not (Test-Path $challengeFile)) {
            $challengeFailureReason = "result file missing at $challengeOwnedPath"
        } else {
            $challengeResultText = Get-Content -Path $challengeFile -Raw
            $verdictMatch = [regex]::Match($challengeResultText, '(?im)^\s*verdict:\s*(agree|disagree)\s*$')
            if (-not $verdictMatch.Success) {
                $challengeFailureReason = "verdict not found in $challengeOwnedPath"
            } else {
                $challengeVerdict = $verdictMatch.Groups[1].Value.ToLowerInvariant()
                Write-RunLog "challenge verdict: $challengeVerdict"
                if ($challengeVerdict -eq "disagree") {
                    $escalateLines = @(
                        "# Codex Challenge Escalation"
                        ""
                        "original orchestrator verdict:"
                        $claudeResponseText
                        ""
                        "codex challenge result file: $challengeOwnedPath"
                        ""
                        "next steps:"
                        "- Read $challengeOwnedPath"
                        "- Compare Codex evidence against the orchestrator verdict above"
                        "- Decide which follow-up spec should be dispatched"
                    )
                    Set-Content -Path $EscalFile -Value ($escalateLines -join "`n")
                    $challengeEscalated = $true
                    $orchDocEntryOverride = "- $Ts UTC -- **challenged** (codex disagrees)"
                } elseif ($challengeVerdict -eq "agree") {
                    $orchDocEntryOverride = "- $Ts UTC -- **no_action** (codex agreed)"
                }
            }
        }

        if ($challengeFailureReason) {
            Write-RunLog "challenge dispatch failed: $challengeFailureReason"
        }
    }
}

# Check for escalation
if ((Test-Path $EscalFile) -or $parsedStatus -eq "escalated" -or $challengeEscalated) {
    Write-RunLog "ESCALATED -- incrementing consecutive_failures"
    $state.consecutive_failures += 1
} else {
    $state.consecutive_failures = 0
}

$state.cumulative_token_input = ([long]$state.cumulative_token_input) + $parsedInputTokens
$state.cumulative_token_output = ([long]$state.cumulative_token_output) + $parsedOutputTokens
Write-RunLog "usage: input=$parsedInputTokens (uncached=$uncachedInput cache_create=$cacheCreate cache_read=$cacheRead) output=$parsedOutputTokens cumulative_input=$($state.cumulative_token_input) cumulative_output=$($state.cumulative_token_output)"

# Update state
# Only update spec/commit tracking on REAL completed dispatches.
# Dry runs and skipped/escalated/no_action runs don't change persistent dispatch state.
$state.last_run_ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
$state.last_run_action = "$($parsedStatus):$($parsedAction)"
$isRealDispatch = ($parsedStatus -eq "completed") -and ($parsedAction -eq "spec_dispatched")
if ($isRealDispatch -and $parsedSpecSlug -and $parsedSpecSlug -ne "null") {
    $state.last_spec_slug = $parsedSpecSlug
    $state.total_specs_dispatched += 1
}
if ($isRealDispatch -and $parsedCommit -and $parsedCommit -ne "null") {
    $state.last_commit_hash = $parsedCommit
}
$state.total_runs += 1

Save-State $state

# Append to orchestrator continuation prompt
$entryParts = @("- $Ts UTC -- **$parsedStatus**")
if ($parsedAction -ne "none" -and $parsedAction -ne "unknown") { $entryParts += "action=$parsedAction" }
if ($parsedSpecSlug) { $entryParts += "spec=$parsedSpecSlug" }
if ($parsedCommit) {
    $shortHash = $parsedCommit.Substring(0, [Math]::Min(7, $parsedCommit.Length))
    $entryParts += "commit=$shortHash"
}
if ($parsedRestart -ne "none" -and $parsedRestart -ne "unknown") { $entryParts += "restarted=$parsedRestart" }
if ($orchDocEntryOverride) {
    Update-Orch-Doc $orchDocEntryOverride
} else {
    Update-Orch-Doc ($entryParts -join " ")
}

# Write the per-run summary
$summaryLines = @(
    "## Run $Ts UTC"
    ""
    "- status: $parsedStatus"
    "- action: $parsedAction"
    "- spec_slug: $parsedSpecSlug"
    "- commit: $parsedCommit"
    "- restarted: $parsedRestart"
    "- duration: $($claudeDur)s"
    "- input_tokens: $parsedInputTokens"
    "- output_tokens: $parsedOutputTokens"
    "- cumulative_input_tokens: $($state.cumulative_token_input)"
    "- cumulative_output_tokens: $($state.cumulative_token_output)"
    "- consecutive_failures: $($state.consecutive_failures)"
    ""
    "See full claude output in $RunLog"
)
Set-Content -Path $RunSummary -Value ($summaryLines -join "`n")

Write-RunLog "=== dev_loop end (status=$parsedStatus, dur=$($claudeDur)s) ==="
exit 0
