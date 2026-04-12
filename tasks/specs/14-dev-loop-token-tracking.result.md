# Spec 14 result -- dev_loop token tracking

Implemented in `scripts/dev_loop.ps1`. Codex landed the bulk of the change (json output format + helper functions + UTC day rollover); CC followed up with a parsing fix (full input footprint instead of just un-cached tokens).

## What changed

1. Switched `claude` invocation from text output to `--output-format json`. Wrapper now receives a structured payload instead of plain stdout.
2. Added helper functions:
   - `Get-JsonPropertyValue` -- recursive BFS over PSObject for a key by name
   - `Convert-JsonNodeToText` -- extracts response text from common field names (`text`, `result`, `response`, `output`, `completion`, `message`, `content`)
   - `Get-ClaudeResponseText` -- top-level wrapper that returns the human-readable narrative
   - `Get-ClaudeTokenCount` -- safe long extraction
3. Parses both the response text (for the YAML summary regex) AND the token usage from the JSON.
4. Saves both to the run log: a `=== claude response text ===` section AND a `=== claude json metadata ===` section with the raw JSON for debugging.
5. **Total input** = `input_tokens` + `cache_creation_input_tokens` + `cache_read_input_tokens`. The original Codex implementation only captured `input_tokens` (the un-cached portion), which was ~0.4% of the real input footprint. Fixed by CC after the dry-run revealed the discrepancy.
6. UTC day rollover: at start of pre-flight, if `last_run_ts` exists and is not on the current UTC date, reset `cumulative_token_input` and `cumulative_token_output` to 0. This makes the daily 320k input gate work as a real cap.
7. Defensive: `Load-State` now adds the `cumulative_token_input` and `cumulative_token_output` fields if missing from old state.json files.

## Verification

Dry run after Codex's change:
```
[2026-04-12T18:55:53Z] usage: input=1091 output=3431 cumulative_input=1091 cumulative_output=3431
```

This was wrong -- 1091 input tokens for 81s of LLM work (reading 12 brain reports + SQLite) is impossible. Inspecting the raw JSON:
```json
"usage": {
    "input_tokens": 1091,
    "cache_creation_input_tokens": 46976,
    "cache_read_input_tokens": 215811,
    "output_tokens": 3431
}
```

Real input footprint: 263,878 tokens. After CC's fix, the wrapper now sums all 3 categories.

## Daily budget impact

At ~264k input tokens per run x 4 scheduled runs/day = ~1.05M tokens/day. The 320k/day gate I originally set is too low -- a single run blows past it. Either:
- Raise the cap to 1.5M/day (current baseline + headroom)
- Or accept that the gate will fire after 1-2 runs and skip the rest of the day

This is informational. The wrapper now correctly TRACKS the budget; whether to raise the cap is a separate decision deferred to the user.

## Files changed

- `scripts/dev_loop.ps1` -- token parsing + UTC day rollover + helper functions + cache-aware input total

## Tests

No new pytest tests (PowerShell wrapper, not Python). Verification was done via dry-run + raw JSON inspection.

## Follow-up

- Decide whether to raise the daily token cap from 320k to 1.5M (or remove the cap entirely since user is on Max sub)
- Optional: log per-run cost from `total_cost_usd` field for visibility
