# Spec 21 -- Per-run cost tracking + raise daily token cap

## Problem

Two related wrapper deficiencies discovered after spec 14 landed:

1. **Daily token cap is way too low.** The wrapper has a hardcoded 320k input tokens/day cap. After spec 14 corrected the parsing to sum un-cached + cache_create + cache_read, a single run consumes ~250-300k input tokens. The cap trips after one run.
2. **Per-run cost is invisible.** The `claude --print --output-format json` payload includes a `total_cost_usd` field but the wrapper doesn't capture it. The user is on Max sub so dollar cost is $0, but cost-per-run is still useful for capacity planning if the loop ever runs against the API directly.

## Desired outcome

The wrapper:
1. Captures `total_cost_usd` from each claude run and records it in state.json (cumulative per UTC day, with daily rollover)
2. Logs cost in the per-run summary line and the run summary file
3. Has a more realistic daily input token cap (1.5M, with a comment explaining the rationale)

## Acceptance criteria

1. `scripts/dev_loop.ps1` adds:
   - `Get-ClaudeCostUsd` helper that pulls `total_cost_usd` from the JSON payload (use the existing `Get-JsonPropertyValue` BFS helper). Returns `[double]0` on failure.
   - `cumulative_cost_usd` field in the default state.json schema and the `Load-State` defensive add.
   - At post-flight token-update time: also update `state.cumulative_cost_usd += parsedCostUsd`. Reset on UTC day rollover (alongside the existing token reset).
2. The "usage:" log line includes the parsed cost and the new cumulative:
   ```
   usage: input=232082 (uncached=82 cache_create=35949 cache_read=196124) output=4622 cumulative_input=233173 cumulative_output=8053 cost=$0.49 cumulative_cost=$0.98
   ```
3. The per-run summary file `state/dev-loop/runs/<ts>.summary.md` adds a `cost_usd:` field.
4. The daily input token cap is raised from 320,000 to **1,500,000** with a comment explaining: "1.5M = ~5 runs/day at ~300k input each. Keeps the loop from blowing up on a runaway day without arbitrarily blocking normal operation."
5. Wrapper still parses cleanly via PSParser.
6. After the patch, fire `pwsh -File scripts/dev_loop.ps1 -DryRun -Force` and verify:
   - "usage:" log line shows `cost=$X.XX cumulative_cost=$Y.YY`
   - state.json has the new `cumulative_cost_usd` field populated
   - state.json has `cumulative_token_input` < 1500000 budget gate

## Non-goals

- Do not add a separate cost-based budget gate (the token gate is sufficient)
- Do not change cost units (USD only, no per-thousand-token normalization)
- Do not log per-run cost in the orchestrator doc (state.json is enough)
- Do not backfill historical state.json files

## Files in scope

- `scripts/dev_loop.ps1`
- `tasks/specs/21-dev-loop-cost-tracking-cap-raise.result.md`

## Evidence

- Spec 14 result file noted both the cap-too-low and cost-not-tracked issues as follow-ups
- Today's dry runs show `cost=$0.49` per run (~250-300k tokens) which the current 320k cap would cut off after the first fire
