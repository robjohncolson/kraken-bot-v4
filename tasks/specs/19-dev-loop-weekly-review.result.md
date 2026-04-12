# Spec 19 Result

## Changes
- `scripts/dev_loop.ps1`: added `-PromptFile` to the param block with the tactical prompt as the default, resolves repo-relative overrides, preserves the existing default prompt behavior, and logs `prompt: <resolved path>` before loading.
- `scripts/dev_loop_weekly_prompt.md`: added a weekly review prompt that mirrors the tactical structure, uses 7-day observation windows, swaps in the weekly pattern-priority list, and explicitly allows strategy parameter changes on weekly runs.
- `scripts/register_dev_loop_weekly_task.ps1`: added the weekly Task Scheduler registration script for `KrakenBot-CcOrchestrator-Weekly`, firing Sundays at 10:00 local time and invoking `dev_loop.ps1 -PromptFile scripts/dev_loop_weekly_prompt.md`.

## Impact note
- GitNexus `impact` / `context` calls were cancelled in this subagent session, so I used a manual blast-radius fallback.
- Manual fallback: `scripts/dev_loop.ps1` is the existing orchestrator wrapper used by the tactical scheduler; the code change is limited to prompt-file selection and logging. The new weekly prompt and weekly registration script are additive and do not change the tactical prompt, tactical task, wrapper gates, or shared `state.json` behavior.

## Verification
- Not run. Subagent mode instructed: do not run verification commands, tests, lint checks, or post-patch parser checks.
- Requested but not executed: the two `[System.Management.Automation.PSParser]::Tokenize(...)` commands and the manual `-PromptFile ... -DryRun -Force` wrapper invocation.
