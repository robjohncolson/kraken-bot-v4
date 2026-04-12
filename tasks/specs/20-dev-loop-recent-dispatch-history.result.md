# Spec 20 Result

## Changes
- `scripts/dev_loop.ps1`: added `Get-RecentDispatchHistory`, which reads `CONTINUATION_PROMPT_cc_orchestrator.md`, finds the `## Run log` section, parses recent run-log entries with either wrapper-style `yyyyMMdd_HHmmss` timestamps or ISO timestamps, keeps only the last 7 days, and returns newest-first objects with `ts`, `status`, `action`, `spec`, `commit`, and `restarted`.
- `scripts/dev_loop.ps1`: after logging `last_code_commit_ts`, now logs the recent-dispatch injection count, builds a `## RECENT DISPATCH HISTORY (last 7 days, newest first)` markdown section, and appends it into the runtime context block before the prompt body.
- `scripts/dev_loop_prompt.md`: added a Step 2 note telling the tactical loop to avoid dispatching a spec slug or action that conceptually duplicates something already dispatched in the last 7 days.
- `scripts/dev_loop_weekly_prompt.md`: added the same Step 2 note for the weekly review flow.

## Helper snippet
```powershell
function Get-RecentDispatchHistory {
    param(
        [int]$daysWindow = 7
    )

    if (-not (Test-Path -LiteralPath $OrchDoc)) {
        return @()
    }

    try {
        $lines = @(Get-Content -LiteralPath $OrchDoc)
    } catch {
        return @()
    }

    $runLogIndex = -1
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^\s*## Run log\s*$') {
            $runLogIndex = $i
            break
        }
    }

    if ($runLogIndex -lt 0) {
        return @()
    }

    $parseStyles = [System.Globalization.DateTimeStyles]::AssumeUniversal -bor [System.Globalization.DateTimeStyles]::AdjustToUniversal
    $cutoff = (Get-Date).ToUniversalTime().AddDays(-$daysWindow)
    $parsedEntries = @()

    for ($i = $runLogIndex + 1; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $entryMatch = [regex]::Match($line, '^- (?<ts>\S+) UTC -- \*\*(?<status>[^*]+)\*\*(?<rest>.*)$')
        if (-not $entryMatch.Success) {
            continue
        }

        $rawTs = $entryMatch.Groups['ts'].Value
        $parsedTs = [DateTimeOffset]::MinValue
        $parsedOk = $false

        if ([DateTimeOffset]::TryParseExact(
                $rawTs,
                'yyyyMMdd_HHmmss',
                [System.Globalization.CultureInfo]::InvariantCulture,
                $parseStyles,
                [ref]$parsedTs
            )) {
            $parsedOk = $true
        } elseif ([DateTimeOffset]::TryParse(
                $rawTs,
                [System.Globalization.CultureInfo]::InvariantCulture,
                $parseStyles,
                [ref]$parsedTs
            )) {
            $parsedOk = $true
        }

        if (-not $parsedOk) {
            continue
        }

        $entryUtc = $parsedTs.ToUniversalTime()
        if ($entryUtc -lt $cutoff) {
            continue
        }

        $fields = @{
            action    = $null
            spec      = $null
            commit    = $null
            restarted = $null
        }

        $remainder = $entryMatch.Groups['rest'].Value.Trim()
        while ($remainder.Length -gt 0) {
            $tokenMatch = [regex]::Match(
                $remainder,
                '^(?:\[(?<bracketKey>action|spec|commit|restarted)=(?<bracketValue>[^\]]*)\]|(?<plainKey>action|spec|commit|restarted)=(?<plainValue>\S+))(?:\s+|$)'
            )
            if (-not $tokenMatch.Success) {
                break
            }

            $key = if ($tokenMatch.Groups['bracketKey'].Success) {
                $tokenMatch.Groups['bracketKey'].Value
            } else {
                $tokenMatch.Groups['plainKey'].Value
            }
            $value = if ($tokenMatch.Groups['bracketValue'].Success) {
                $tokenMatch.Groups['bracketValue'].Value
            } else {
                $tokenMatch.Groups['plainValue'].Value
            }
            $fields[$key] = if ($value -eq '') { $null } else { $value }
            $remainder = $remainder.Substring($tokenMatch.Length).TrimStart()
        }

        $parsedEntries += [PSCustomObject]@{
            sort_ts   = $entryUtc
            ts        = $rawTs
            status    = $entryMatch.Groups['status'].Value
            action    = $fields.action
            spec      = $fields.spec
            commit    = $fields.commit
            restarted = $fields.restarted
        }
    }

    if ($parsedEntries.Count -eq 0) {
        return @()
    }

    $sortedEntries = @(
        $parsedEntries |
            Sort-Object -Property sort_ts -Descending |
            ForEach-Object {
                [PSCustomObject]@{
                    ts        = $_.ts
                    status    = $_.status
                    action    = $_.action
                    spec      = $_.spec
                    commit    = $_.commit
                    restarted = $_.restarted
                }
            }
    )

    return $sortedEntries
}
```

## Impact note
- GitNexus impact calls were attempted in this subagent session, but the MCP requests were cancelled before returning a graph result.
- Manual blast-radius fallback: the wrapper change is limited to runtime-context assembly in `scripts/dev_loop.ps1`, plus Step 2 guidance text in the two prompt files. It does not change state persistence, dispatch parsing, restart behavior, or orchestrator writeback semantics.

## Verification
- Not run. Subagent mode instructed: do not run verification commands, tests, lint checks, dry runs, or post-patch PSParser checks.
