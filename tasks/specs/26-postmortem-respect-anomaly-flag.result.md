Implemented Spec 26 in `scripts/cc_brain.py`.

- Added `run_postmortem_step()` and `_split_postmortem_outcomes()` so Step 4 filters out any trade outcome with a truthy `anomaly_flag` before computing 7-day trade count, wins, P&L, loser analysis, and self-tune inputs.
- Added an `Anomalies (excluded from stats)` section that logs each excluded row with its `pair`, `net_pnl`, and `anomaly_flag`.
- Updated `run_brain()` to delegate Step 4 to the new helper so the display, immediate post-mortem, and self-tune path all use the same filtered dataset.
- Added `tests/test_cc_brain_postmortem_filter.py` covering both the filtered summary output and the filtered outcomes passed into `self_tune()`.
- Verified with `curl.exe -s "http://127.0.0.1:58392/api/trade-outcomes?lookback_days=7"` that the live `/api/trade-outcomes` response already includes `anomaly_flag`, so `web/routes.py` did not need a code change.

Validation was not run in this subagent because the task wrapper explicitly prohibited tests, lint, and other verification commands.

GitNexus impact analysis was attempted before editing `run_brain`, but the GitNexus MCP calls were cancelled in this subagent context.
