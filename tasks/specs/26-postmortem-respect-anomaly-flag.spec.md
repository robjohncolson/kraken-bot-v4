# Spec 26 -- Brain post-mortem respects trade_outcomes.anomaly_flag

## Problem

Spec 11 added the `trade_outcomes.anomaly_flag` column to mark rows like the USDT phantom (id=1) as anomalous without rewriting the historical P&L. The orchestrator's health snapshot (spec 22+23) correctly excludes these rows when computing `net_pnl_7d` -- the snapshot shows **$1.26 underlying** vs the raw `-$14.59`.

But `scripts/cc_brain.py`'s post-mortem step still reads raw `trade_outcomes` rows without filtering by `anomaly_flag`. Result:

```
Last 7 days: 14 trades, 6 wins, P&L=$-14.5879
  PM: USDT/USD lost $15.8498 (root_exit_bearish, 0.0h) -- no_pattern
No tuning needed (WR=43%, P&L=$-14.59, 14 trades)
```

The phantom is still in the brain's view. This means:

1. The brain's self-tune logic is reading a wrong P&L number
2. The brain's reasoning ("no_pattern") on the anomalous row is meaningless
3. The brain's deep_postmortem feature might be triggered on wrong data
4. Any human reading the brain report sees the misleading number

## Desired outcome

The brain's post-mortem logic SKIPS rows with `anomaly_flag` set when computing 7-day stats and individual trade analyses. The post-mortem display either omits anomaly-flagged rows entirely OR shows them in a separate "anomalies (excluded)" section.

## Acceptance criteria

1. In `scripts/cc_brain.py`, find the post-mortem step (Step 4 in the brain cycle, around the "Last 7 days: N trades, M wins, P&L=$X" line). It currently fetches trade outcomes via `/api/trade-outcomes?lookback_days=7`.
2. Modify the consumer to filter `outcomes` by `anomaly_flag` -- treat any truthy `anomaly_flag` value as "exclude from stats".
3. Recompute the displayed numbers:
   - Total trades: count of NON-anomalous rows
   - Wins: count of NON-anomalous rows where net_pnl > 0
   - P&L sum: SUM of net_pnl on NON-anomalous rows
4. Optionally show anomalies separately: after the existing 7-day summary line, if any anomalies exist:
   ```
   Anomalies (excluded from stats): N rows
     ANOMALY: USDT/USD net_pnl=$-15.85 (anomaly_flag=stablecoin_unit_mismatch)
   ```
5. The self-tune block at the end of post-mortem must use the FILTERED numbers, not raw.
6. Verify `/api/trade-outcomes` actually returns the `anomaly_flag` field in its JSON. If not, also need a small `web/routes.py` change to surface it. Check by `curl http://127.0.0.1:58392/api/trade-outcomes?lookback_days=7` and inspecting the response.
7. Add a regression test in `tests/test_cc_brain_postmortem_filter.py`:
   - Mock `/api/trade-outcomes` to return 3 trades, one with `anomaly_flag='stablecoin_unit_mismatch'`
   - Run the post-mortem step
   - Assert displayed total trades = 2 (not 3)
   - Assert displayed P&L = sum of the 2 non-anomaly rows
8. Full pytest suite green.

## Non-goals

- Do not delete or rewrite the anomaly-flagged rows themselves.
- Do not add a NEW anomaly_flag value. Spec 11 already has `stablecoin_unit_mismatch`. If new types are needed later, that's a separate spec.
- Do not change the orchestrator's health snapshot (already correct via spec 22+23).
- Do not modify the postmortem deep_dive logic if it lives in a different file.

## Files in scope

- `scripts/cc_brain.py`
- `web/routes.py` (only if `anomaly_flag` is not already in the API response)
- `tests/test_cc_brain_postmortem_filter.py` (new)
- `tasks/specs/26-postmortem-respect-anomaly-flag.result.md`

## Evidence

- `state/cc-reviews/brain_2026-04-12_2259.md` line 75-83: brain post-mortem still shows the `-$14.59` and the `$15.85 USDT/USD` row
- `data/bot.db trade_outcomes id=1`: the anomaly_flag should be set on this row (spec 11 schema migration)
- The orchestrator's `dev_loop_health_snapshot.py` already respects `anomaly_flag` and reports the correct `net_pnl_7d=$1.26`
