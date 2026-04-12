# Spec 20 -- Inject recent-dispatch history into the runtime context

## Problem

The orchestrator currently has minimal cross-run memory. `state.json` stores only `last_spec_slug` (a single slot). The chronological run log in `CONTINUATION_PROMPT_cc_orchestrator.md` has every entry but the LLM doesn't read it as part of Step 1 Observe.

Failure modes this enables:
1. The LLM proposes a slug that was dispatched 2 runs ago (not the most recent, so the same-slug guard misses it)
2. The LLM doesn't know its own track record (which past dispatches landed cleanly vs were challenged vs escalated)
3. The LLM can't reason about "I tried this approach last week, it didn't help, try something else"

## Desired outcome

The wrapper injects a "recent dispatch history" section into the runtime context block alongside `last_code_commit_ts`. The LLM gets a quick view of what's been tried in the last 7 days, with status, so it can avoid duplicates and reason about its own track record.

## Acceptance criteria

1. `scripts/dev_loop.ps1` adds a helper `Get-RecentDispatchHistory` that:
   - Reads the chronological run log section of `CONTINUATION_PROMPT_cc_orchestrator.md` (the section after `## Run log`)
   - Parses entries from the last 7 days (filter by the timestamp at the start of each line)
   - Returns a structured list of `{ts, status, action, spec_slug, commit, restarted}` records
   - If the file is missing or has 0 matching entries, returns an empty list (NOT an error)
2. The runtime context block built before invoking claude now includes a "Recent dispatches (last 7 days)" subsection:
   ```
   ## RUNTIME CONTEXT (injected by wrapper)
   - last_code_commit_ts: <iso>
   - Brain reports / memories with timestamps BEFORE this point reflect the OLD code...

   ## RECENT DISPATCH HISTORY (last 7 days, last entry first)
   - <ts> -- status=<status> action=<action> spec=<slug> commit=<sha7> restarted=<...>
   - <ts> -- status=<status> ...
   (or "No dispatches in the last 7 days." if empty)
   ```
3. `scripts/dev_loop_prompt.md` Step 2 (Diagnose) gains a note IMMEDIATELY before the priority list:
   - "Before picking a target, scan the RECENT DISPATCH HISTORY section in the runtime context. If your candidate spec slug or action conceptually matches one already dispatched in the last 7 days, pick something else (or set status=no_action with a reason citing the prior dispatch)."
4. `scripts/dev_loop_weekly_prompt.md` gets the same note (weekly cares more about cross-run patterns than tactical).
5. The injection is unconditional -- both DryRun and live runs see the history.
6. Wrapper still parses cleanly via PSParser tokenization.
7. After the patch, fire a manual dry run with `-Force` and confirm the run log shows:
   - "injecting recent dispatch history (N entries)" log line
   - The runtime context block in the run log file contains the dispatch history section

## Non-goals

- Do not parse the orchestrator doc's Bring-up section (only the chronological run log section)
- Do not store the recent history in state.json (it's derived from the doc each run)
- Do not enforce uniqueness in code (the LLM is the enforcer; the wrapper just provides context)
- Do not change the YAML output format

## Files in scope

- `scripts/dev_loop.ps1` (Get-RecentDispatchHistory helper + injection)
- `scripts/dev_loop_prompt.md` (Step 2 note)
- `scripts/dev_loop_weekly_prompt.md` (Step 2 note)
- `tasks/specs/20-dev-loop-recent-dispatch-history.result.md` (result file)

## Evidence

- `CONTINUATION_PROMPT_cc_orchestrator.md` has the run log structure the wrapper needs to parse
- `state/dev-loop/state.json` has only the LAST slug -- inadequate for cross-run reasoning
