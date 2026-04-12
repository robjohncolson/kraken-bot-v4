# Spec 17 Result

## Changes
- `scripts/dev_loop.ps1`: after the pre-flight gates, the wrapper now resolves the most recent `HEAD` commit that touched a `.py` file, converts it to UTC ISO, logs `injecting last_code_commit_ts=...`, and prepends a runtime context block before the existing dry-run override.
- `scripts/dev_loop_prompt.md`: Step 2 now tells the orchestrator to count recurring patterns only from observations with `timestamp > last_code_commit_ts`, while treating earlier observations as pre-fix history.
- The existing settled gate logic and dry-run override ordering were left intact.

## Snippet
```powershell
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
```

## Verification
- Not run. Subagent mode instructed: do not run verification commands, tests, or lint checks after applying patches.
- Requested but not executed: `[System.Management.Automation.PSParser]::Tokenize(...)`
