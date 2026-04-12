# Spec 17 -- Time-window the dev_loop observation step

## Problem

The CC Orchestrator (`scripts/dev_loop.ps1`) reads brain reports and SQLite memories without filtering by "what's actually still current after the most recent code change." Concrete failure mode observed today:

- After spec 12 (permissions blacklist) shipped at ~17:30 UTC, the bot was restarted at 18:27 UTC
- A dry run of the orchestrator at 18:54 UTC read brain reports from 04:40 -> 17:22 UTC and counted "15+ consecutive AUD/USD permission failures"
- Every single one of those failures was PRE-fix. They cannot recur because spec 12 now blocks them
- The orchestrator proposed dispatching a NEW spec (`blacklist-restricted-fiat-pairs`) to fix what was already fixed

The wrapper has a "settled" gate that uses git commit timestamps for the dispatch decision, but the LLM's observation step doesn't filter that way -- it treats the whole 12-cycle history as equally current.

## Desired outcome

The orchestrator only counts pathology that occurred AFTER the most recent commit affecting the bot's runtime code. Historical observations are explicitly labeled "pre-fix, already addressed" in the prompt context.

## Acceptance criteria

1. `scripts/dev_loop.ps1` computes `lastCodeCommitTs` before invoking claude:
   - Most recent git commit on the current branch that touched at least one `.py` file
   - Timestamp in UTC ISO format (`yyyy-MM-ddTHH:mm:ssZ`)
   - Implementation: `git log -1 --format=%ct -- '*.py'` (or equivalent that filters by file extension)
2. The wrapper injects this into the prompt as a clearly-labeled context line, e.g. prepending or appending:
   ```
   ## RUNTIME CONTEXT (injected by wrapper)
   - last_code_commit_ts: 2026-04-12T18:30:15Z
   - Brain reports / memories with timestamps BEFORE this point reflect the OLD code and may show pathology that has already been fixed. When counting "recurring patterns" for priority rules 2/4/5/6, ONLY count occurrences with timestamp > last_code_commit_ts.
   ```
3. `scripts/dev_loop_prompt.md` Step 2 (Diagnose) is updated with an explicit instruction:
   - "When counting recurring patterns (rules 2, 4, 5, 6, 7), the wrapper has injected `last_code_commit_ts` in the runtime context block. Only count pattern occurrences from brain reports / memories with `timestamp > last_code_commit_ts`. Earlier observations are PRE-FIX HISTORY -- noted, not counted."
4. The dry-run override remains independent of this change.
5. After the patch, fire a dry run with `-Force` and confirm:
   - Wrapper logs `injecting last_code_commit_ts=<ts>`
   - Run log shows the prompt now contains the runtime context block
   - Claude's response references the timestamp constraint
6. The wrapper still parses cleanly via PSParser tokenization.

## Non-goals

- Do not change the "settled" gate logic (it already uses commit timestamps correctly for dispatch decisions)
- Do not change the YAML output format
- Do not add the timestamp to state.json (it's a per-run input, not persistent state)
- Do not change the Step 1 Observe instructions -- claude should still READ all 12 brain reports, just count selectively in Step 2

## Files in scope

- `scripts/dev_loop.ps1` (compute + inject)
- `scripts/dev_loop_prompt.md` (Step 2 instruction)
- `tasks/specs/17-dev-loop-time-window-observation.result.md` (result file)

## Evidence

- `state/dev-loop/runs/20260412_185432.log` -- the dry run that proposed `blacklist-restricted-fiat-pairs` based on entirely pre-fix data
- `state/dev-loop/runs/20260412_182741.log` -- the live run that correctly said `no_action` when re-reading the same data 1 minute later (LLM stochasticity is the residual issue spec 18 will address)
