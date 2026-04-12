# Spec 19 -- Weekly review run for the CC Orchestrator

## Problem

The 6-hourly tactical loop (specs 11-18) is good at catching per-cycle pathology -- recurring errors, accounting bugs, single-occurrence anomalies. It is NOT good at:

- Multi-day strategy trends (e.g. "ENTRY_THRESHOLD has been ratcheting up for 3 days, fees are still 84% of wins")
- Pattern-level findings that require comparing distributions over a week
- Strategy parameter drift that the tactical loop is explicitly told not to touch
- Slow-burning issues that don't trip the priority list on any single 6h fire

The user wants a separate weekly review that takes a 7-day horizon and focuses on pattern-level analysis.

## Desired outcome

A new scheduled task `KrakenBot-CcOrchestrator-Weekly` that fires once a week (e.g. Sunday 10:00 PT) and runs the same wrapper with a different prompt focused on weekly pattern analysis. The weekly run is allowed to propose strategy parameter changes, which the tactical loop cannot.

## Acceptance criteria

1. `scripts/dev_loop.ps1` accepts a new parameter `-PromptFile <path>`:
   - Default: `scripts/dev_loop_prompt.md` (the existing tactical prompt)
   - When passed, the wrapper loads from the given path instead
   - All other behavior (gates, post-flight, challenge, state.json, run logs, orchestrator doc) is unchanged
2. New file `scripts/dev_loop_weekly_prompt.md`:
   - Same structure as the tactical prompt (Steps 1-7, hard rules, YAML output format)
   - Step 1 Observe: tail 7 days of brain reports, query 7-day trade_outcomes, read 7-day cc_memory, look at 7d git log
   - Step 2 Diagnose: NEW priority list focused on PATTERNS
     1. Trend in win rate over 7d (e.g. dropped > 10 percentage points week-over-week)
     2. Strategy parameter drift (`ENTRY_THRESHOLD`, `MAX_POSITION_USD`, etc. moving systematically)
     3. Fee burden trend (fees as % of gross wins climbing or falling)
     4. New asset categories appearing in trades that weren't there last week
     5. Reconciliation discrepancy patterns (multiple distinct types over 7d, not just count)
     6. Self-tune rule firing/not-firing patterns
     7. Shadow vs live divergence over 7d
     8. Anything from the existing tactical priority list that has been recurring for the WHOLE week
   - Step 3 Decide: same one-spec-per-run rule, but allowed to propose strategy parameter changes (which the tactical loop is forbidden from)
   - Steps 4-7: same as tactical (write spec/plan, dispatch Codex, verify, restart, document)
   - Hard rules same as tactical PLUS: "weekly runs may propose strategy parameter changes; tactical runs may not"
3. New file `scripts/register_dev_loop_weekly_task.ps1`:
   - Similar to `register_dev_loop_task.ps1` but registers `KrakenBot-CcOrchestrator-Weekly`
   - Trigger: weekly, Sunday 10:00 PT, no repetition (just once per week)
   - Action: invokes `dev_loop.ps1 -PromptFile scripts/dev_loop_weekly_prompt.md`
   - Same time limit, principal, settings as the tactical task
4. The weekly task does NOT replace the tactical task. Both coexist.
5. The wrapper still parses cleanly via PSParser tokenization.
6. Test the parameter with a manual invocation:
   - `pwsh -File scripts/dev_loop.ps1 -PromptFile scripts/dev_loop_weekly_prompt.md -DryRun -Force`
   - Confirm the run log shows the weekly prompt was loaded
   - Confirm the wrapper completes without errors

## Non-goals

- Do not implement weekly-vs-tactical state separation. Both runs share state.json. (If needed later, add a `-StateFile` parameter mirror of `-PromptFile`.)
- Do not enforce the "weekly may propose strategy changes" rule in code. It's a prompt-level convention.
- Do not change the existing tactical prompt or its scheduled task.
- Do not address how the weekly + tactical loop interact when they fire in the same window. They run sequentially via Task Scheduler, the gates handle conflicts.

## Files in scope

- `scripts/dev_loop.ps1` (-PromptFile parameter)
- `scripts/dev_loop_weekly_prompt.md` (new)
- `scripts/register_dev_loop_weekly_task.ps1` (new)
- `tasks/specs/19-dev-loop-weekly-review.result.md` (result file)

## Cost note

One additional run per week, ~300k input tokens. Negligible against the daily tactical budget.

## Evidence

- `state/dev-loop/runs/20260412_182741.log` -- live run noted "ENTRY_THRESHOLD self-tune ratcheting 0.6 -> 0.65 -> 0.7 within two cycles (fees=84% of wins) -- strategy parameter, out of scope" -- exactly the kind of pattern the weekly run should be allowed to address
