# Spec 30 -- Orchestrator wrapper self-unblock

## Problem

The Layer 3 CC Orchestrator wrapper (`scripts/dev_loop.ps1`) appends a run-log entry to `CONTINUATION_PROMPT_cc_orchestrator.md` on every completed run (see line ~280 where `$OrchDoc` is appended). That file is tracked in git. On the next scheduled fire, the wrapper's pre-flight "unstaged user changes" gate at line 537-542 sees the modified file, classifies it as "user is mid-edit", and calls `Exit-NoAction` -- skipping the run entirely.

Net effect: the wrapper self-blocks after the first run that produces an appended entry. Today's orchestrator log shows three consecutive skipped fires (07:15, 13:15, 19:15 UTC on 2026-04-13) all with the same `unstaged user changes present (user is mid-edit)` reason, before the user manually committed the pending entries. This is a permanent deadlock, not a transient one -- without intervention, the orchestrator never fires again.

The gate is correct in principle (don't stomp user mid-edits), but it treats the wrapper's own writes as user edits. The wrapper owns `CONTINUATION_PROMPT_cc_orchestrator.md`, so its own writes to that file should not trip the gate.

## Desired outcome

1. The wrapper no longer self-blocks when the only unstaged change is the wrapper's own append to `CONTINUATION_PROMPT_cc_orchestrator.md`.
2. Genuine user edits to other files (e.g., `main.py`, `runtime_loop.py`, `scripts/dev_loop.ps1` itself) still trip the gate.
3. Run-log history is preserved in git (not thrown away or moved to a gitignored location).
4. The fix survives `-Force` and `-DryRun` modes correctly.

## Acceptance criteria

1. **Auto-commit the wrapper's own writes in post-flight** before the run exits successfully. In `scripts/dev_loop.ps1`, at the point where the wrapper has finished appending to `CONTINUATION_PROMPT_cc_orchestrator.md` (and any other wrapper-owned files like `state/dev-loop/state.json` which is already gitignored per the current state), run:
   ```powershell
   git add CONTINUATION_PROMPT_cc_orchestrator.md
   if (git diff --cached --quiet -- CONTINUATION_PROMPT_cc_orchestrator.md) {
       # no changes to commit
   } else {
       git commit -m "docs(orchestrator): append run log $Ts"
   }
   ```
   The commit must succeed without any pre-commit hooks failing. If the hook is running `pytest` or similar, the post-flight phase must tolerate it (the wrapper already runs after the main claude call, so pytest will see the state claude left behind).
   Use a single-line subject matching the style of past commits (`docs(orchestrator): ...`). Do NOT push.

2. **Exclude the orchestrator doc from the pre-flight gate as a belt-and-suspenders**. At line 537-542 where `git status --porcelain` is parsed for modified files, add a filter that excludes `CONTINUATION_PROMPT_cc_orchestrator.md` from the set of "unstaged user changes" before the gate triggers `Exit-NoAction`. Approach: keep the grep/parse as-is, but after getting the list of modified paths, remove `CONTINUATION_PROMPT_cc_orchestrator.md` from the list. Only trip the gate if the filtered list is non-empty.
   Rationale: auto-commit (criterion 1) is the primary fix, but if a commit ever fails for any reason, the gate must not permanently deadlock on the wrapper's own file.

3. **Gate must still trip on real user edits**: if `main.py` or any other file besides `CONTINUATION_PROMPT_cc_orchestrator.md` is modified, the gate still exits with `unstaged user changes present`. Verify with a manual test scenario documented in the result file:
   - Scenario A: only `CONTINUATION_PROMPT_cc_orchestrator.md` modified -> gate passes, wrapper runs claude, post-flight auto-commits the doc.
   - Scenario B: `main.py` modified -> gate trips, `Exit-NoAction` fires, wrapper skips.
   - Scenario C: both `main.py` and the orch doc modified -> gate trips (on main.py), wrapper skips and does NOT commit the orch doc.

4. **`-DryRun` must not commit**: when `$DryRun` is set, the post-flight auto-commit is skipped. The wrapper still writes to `CONTINUATION_PROMPT_cc_orchestrator.md` (existing behavior) but leaves the working tree dirty as before. Document this in a comment.

5. **`-Force` must still trip the pre-flight gate exclusion correctly**. `-Force` bypasses gates for manual testing but should NOT change the auto-commit behavior: a successful `-Force` run should still auto-commit its own writes. A `-Force -DryRun` run should still NOT commit (DryRun takes precedence over Force for this).

6. **PowerShell syntax test**: add a pytest in `tests/test_dev_loop_wrapper.py` (create the file if it doesn't exist) that at minimum parses the updated `scripts/dev_loop.ps1` via `pwsh -NoProfile -Command "[scriptblock]::Create((Get-Content -Raw scripts/dev_loop.ps1))"` and asserts the script parses without syntax error. Do not attempt to execute the full wrapper in tests.

7. Full pytest green.

## Non-goals

- Do not move `CONTINUATION_PROMPT_cc_orchestrator.md` to a gitignored location. The file is the run log and needs to stay in git so fresh CC sessions can read it.
- Do not add a rebase or merge step. The commit is a plain append-only commit to master by a single local writer.
- Do not push. The hard rules for Layer 3 forbid pushing to remote.
- Do not modify the prompt file (`scripts/dev_loop_prompt.md`). The fix is purely wrapper-side.
- Do not modify `scripts/register_dev_loop_task.ps1` or the Windows scheduled task registration.
- Do not change the pre-flight gate's behavior on untracked files -- untracked is already ignored and should stay that way.

## Files in scope

- `scripts/dev_loop.ps1` (both the post-flight auto-commit and the pre-flight gate filter)
- `tests/test_dev_loop_wrapper.py` (new file, PowerShell parse test only)
- `tasks/specs/30-orchestrator-self-unblock.result.md`

## Evidence

- `CONTINUATION_PROMPT_cc_orchestrator.md` chronological log shows three consecutive skipped runs on 2026-04-13:
  ```
  - 20260413_071502 UTC -- **skipped** (unstaged user changes present (user is mid-edit))
  - 20260413_131502 UTC -- **skipped** (unstaged user changes present (user is mid-edit))
  - 20260413_191502 UTC -- **skipped** (unstaged user changes present (user is mid-edit))
  ```
- Today's commits `2e8c9d8` (runtime XPU preload) and `295c71c` (docs orchestrator absorb pending run log entries) manually unblocked the loop but do not fix the recurrence.
- `scripts/dev_loop.ps1:33` defines `$OrchDoc`, `scripts/dev_loop.ps1:~280` appends to it, `scripts/dev_loop.ps1:537-542` is the gate that self-trips.
