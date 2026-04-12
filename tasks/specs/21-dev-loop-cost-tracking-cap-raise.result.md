# Spec 21 Result

- Added `Get-ClaudeCostUsd` to `scripts/dev_loop.ps1` to parse Claude JSON cost fields as USD doubles.
- Extended `Load-State` so `cumulative_cost_usd` is bootstrapped for new state and backfilled for older state files.
- Reset `cumulative_cost_usd` on UTC day rollover, accumulate per-run cost after each Claude invocation, and include both per-run and cumulative cost in the `usage:` log line.
- Added `cost_usd` and `cumulative_cost_usd` to each per-run summary markdown file.
- Raised the daily input token cap from `320000` to `1500000` and updated the inline rationale comment.
