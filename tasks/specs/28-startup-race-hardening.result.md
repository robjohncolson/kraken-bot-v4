Implemented Spec 28 in the owned runtime and test files.

- `runtime_loop.py`
  - Moved `run_once()` startup ordering so `_ensure_websocket_connected()`, `_ensure_subscriptions()`, and `_reap_stale_cc_orders(now)` run before `scheduler.run_cycle(...)`.
  - Added one-shot `MissingCurrentPriceError` recovery in `run_once()`: log the missing pair, fetch a REST OHLC close for that pair, seed `current_prices` via `replace(...)`, and retry the scheduler once.
  - Added `_seed_current_price_from_rest()` for the retry path. It only seeds missing pairs and keeps the emergency startup seed logic intact.
  - Changed stale-order memory persistence to write the `stale_order_cancelled` row directly into `cc_memory` with the cycle timestamp (`now.isoformat()`), while keeping the write non-fatal if SQLite insert fails.

- `tests/test_runtime_loop.py`
  - Added `test_run_once_reaper_runs_before_scheduler`.
  - Added `test_run_once_recovers_from_missing_price`.
  - Renamed the single-row stale-order memory assertion to `test_reaper_writes_stale_order_cancelled_memory` and tightened it to assert the stored timestamp matches the cycle time.

- Verification
  - Per subagent instructions, I did not run pytest.
  - Per subagent instructions, I did not run parser verification commands.

- Impact analysis
  - GitNexus MCP calls were attempted for `run_once` and `_reap_stale_cc_orders`, but this subagent environment returned `user cancelled MCP tool call` for every request.
  - Manual fallback used instead:
    - `run_once()` remains the only per-cycle orchestration entrypoint (`run_forever()` and direct runtime tests call it).
    - `_reap_stale_cc_orders()` is still isolated to the runtime loop and only changes local order cancellation and memory persistence behavior before scheduler execution.
