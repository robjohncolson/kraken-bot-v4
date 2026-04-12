Implemented Spec 27 in the owned runtime/config/test files.

- `core/config.py`
  - Added `Settings.cc_order_max_age_minutes` with `CC_ORDER_MAX_AGE_MINUTES` env support.
  - Default is `15`.

- `runtime_loop.py`
  - Added `_reap_stale_cc_orders(now)` to scan persisted `orders` rows where `kind='cc_api'` and `status='open'`.
  - Parses `created_at`, cancels orders at or above the configured age threshold, treats `CancelOrderNotFoundError` as a local-cancel case, and marks SQLite status as `cancelled`.
  - Writes `cc_memory` rows with `category='stale_order_cancelled'` and the requested payload.
  - Removes matching in-memory `cc_api` pending orders when present.
  - Hooks the reaper into `run_once()` before conditional/rotation planning so it runs every cycle, including `cc_brain_mode`.

- `tests/test_runtime_loop.py`
  - Added coverage for stale cancel after threshold.
  - Added coverage for skipping fresh `cc_api` orders.
  - Added coverage for ignoring stale `rotation_entry` orders.
  - Added coverage for `CancelOrderNotFoundError`.
  - Added coverage for one `cc_memory` row per cancelled stale order.

- Verification
  - Per subagent instructions, I did not run `python -m pytest tests/test_runtime_loop.py -x`.
  - Per subagent instructions, I did not run `python -m pytest tests/ -x`.

- Impact analysis
  - GitNexus MCP was unavailable in this subagent environment. Calls returned `user cancelled MCP tool call`.
  - Manual fallback used instead:
    - `run_once()` is the central per-cycle hook, called by `run_forever()` and directly by runtime tests.
    - `_maybe_run_rotation_planner()` is only reached from `run_once()`.
    - The reaper hook was placed in `run_once()` before `_maybe_plan_conditional_rotation()` and `_maybe_run_rotation_planner()` so it executes ahead of internal reproposal paths while remaining isolated from order placement code.
