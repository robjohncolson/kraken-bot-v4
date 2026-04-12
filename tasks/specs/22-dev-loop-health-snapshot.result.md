# Spec 22 Result

## Summary
- Added `scripts/dev_loop_health_snapshot.py` as a standalone Python helper that reads `data/bot.db`, computes the health metrics from SQLite, fetches `/api/balances`, and emits one JSON object.
- Updated `scripts/dev_loop.ps1` to call the helper through `Get-BotHealthSnapshot`, log the computed 7-day trade and P&L summary, and inject a `HEALTH SNAPSHOT` section into the runtime context block after recent dispatch history.
- Updated both dev-loop prompts so Step 1 tells the orchestrator to use the injected health snapshot first and treat brain reports plus direct SQLite reads as follow-up validation.

## Notes
- The Python helper isolates each metric section with local error handling so individual query or HTTP failures degrade to `null` fields instead of aborting the snapshot.
- The wrapper falls back to `## HEALTH SNAPSHOT (unavailable)` when the helper cannot return a parseable snapshot object.
