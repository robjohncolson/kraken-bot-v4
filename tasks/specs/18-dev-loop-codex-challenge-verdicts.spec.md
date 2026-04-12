# Spec 18 -- Codex challenge on no_action verdicts

## Problem

Even with time-windowed observation (spec 17), the LLM can still misclassify a real issue as benign or no_action. Concrete failure mode observed today:

- The orchestrator's first live run at 18:27 UTC said "no_action" and labeled the recurring `untracked_assets=4-5` reconciliation warning as "benign held-fiat accounting noise"
- A follow-up Codex investigation (spec 15) found this was wrong: the assets are actually FLOW/HYPE/MON/TRIA -- intentional CC API holdings whose orders bypass SQLite tracking. Real bug, fixed by spec 16.

The LLM made a plausible but unverified hypothesis ("matches held fiat") and didn't query SQLite to confirm. Codex DID query SQLite and got the right answer.

This is a pattern: claude is good at narrative diagnosis but inconsistent at empirical verification. A second-opinion pass from an independent agent on no_action verdicts would catch this class of mistake.

## Desired outcome

After every successful claude run that returns `status=no_action` (or that explicitly labels a finding as "benign" / "deferred"), the wrapper automatically dispatches Codex via cross-agent.py to investigate and verify or refute the verdict on the specific finding. If Codex disagrees with evidence, the wrapper writes `state/dev-loop/escalate.md` and the next run will halt for user review.

## Acceptance criteria

1. After post-flight YAML parsing in `scripts/dev_loop.ps1`, when `parsedStatus -eq "no_action"`:
   - Extract the most prominent finding from `claudeResponseText`. Heuristic: look for sentences containing "benign", "deferred", "below threshold", "cosmetic", "no action needed". If nothing matches, skip the challenge.
   - If a finding is found, dispatch a Codex investigation:
     ```
     C:/Python313/python.exe /c/Users/rober/Downloads/Projects/Agent/runner/cross-agent.py
       --direction cc-to-codex
       --task-type investigate
       --working-dir <kraken-bot-v4>
       --owned-paths state/dev-loop/challenge-<ts>.md
       --timeout 600
       --prompt "<challenge prompt — see below>"
     ```
   - The challenge prompt should pass:
     - the orchestrator's verdict (the `no_action` justification text)
     - the specific finding being verified
     - explicit instructions: "Read the relevant code and SQLite directly. Verify the verdict OR refute it with evidence. Write your conclusion to `state/dev-loop/challenge-<ts>.md` with a `verdict: agree | disagree` field."
2. Parse the challenge result file. If `verdict: disagree`:
   - Write `state/dev-loop/escalate.md` with: the original orchestrator verdict, Codex's challenge result, and a `next steps` section pointing the user to the relevant files
   - Update state.json: increment `consecutive_failures` (this counts as a "we got it wrong" event)
   - Append to the orchestrator doc: `- <ts> -- **challenged** (codex disagrees: <one-line summary>)`
3. If `verdict: agree`:
   - Append to the orchestrator doc: `- <ts> -- **no_action** (codex agreed)`
   - Do NOT escalate
4. If the challenge dispatch itself fails or times out:
   - Log a warning, do NOT escalate (false alarm risk), keep the no_action result intact
5. The challenge ONLY fires on `no_action` status. Other statuses (`completed`, `escalated`, `error`, `dry_run`, `skipped`) skip the challenge.
6. Add a flag `-SkipChallenge` to the wrapper for testing:
   - `pwsh -File scripts/dev_loop.ps1 -SkipChallenge` -- skip the challenge step entirely
   - Useful when the user wants a fast loop and accepts higher false-benign risk
7. Wrapper still parses cleanly via PSParser.

## Non-goals

- Do not challenge `dry_run` results (they're observational only)
- Do not challenge `completed` results (the spec was implemented, that's its own verification)
- Do not implement a Codex-vs-Codex tiebreaker if the challenge result file is malformed
- Do not implement automatic re-dispatch of a NEW spec when challenge disagrees -- that's the next session's call
- Do not add a token-budget gate specifically for challenges (use the existing daily cap)

## Files in scope

- `scripts/dev_loop.ps1` (post-flight challenge logic)
- `tasks/specs/18-dev-loop-codex-challenge-verdicts.result.md` (result file)

The prompt file does NOT need to change for this spec.

## Cost note

Each challenge adds one cross-agent.py invocation per `no_action` run. At ~30s each, that's a few extra seconds per fire and one Codex turn (~10-30k tokens). Across 4 scheduled runs/day the marginal cost is small, and the benefit (catching the next untracked_assets-class bug) is high.

## Evidence

- `state/dev-loop/runs/20260412_182741.log` -- the no_action run that called untracked_assets benign
- `tasks/specs/15-untracked-assets-investigation.result.md` -- Codex's follow-up that proved the verdict wrong
- `tasks/specs/16-persist-cc-api-orders.result.md` -- the fix that landed because of Codex's challenge
