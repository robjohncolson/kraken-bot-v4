# Spec 28 -- Startup race hardening + spec 27 reaper memory write fix

## Problem

Three related issues uncovered when spec 27 deployed at 00:06 UTC today:

1. **Startup price-deadlock**: when a CC API order had filled between bot restarts, the rotation tree gained a root for the new asset but the WebSocket layer hadn't subscribed yet. The first cycle's `scheduler.run_cycle()` raised `MissingCurrentPriceError`, was caught at line 568, and aborted before `_ensure_subscriptions()` at line 575 could run. Tight-loop deadlock. Patched today via emergency commit `66d72e4` (seed REST-fetched prices into `current_prices` at startup) but the fix is tactical -- the deeper issue is that the reaper, subscription logic, and scheduler are in the wrong order.

2. **Spec 27 reaper memory write silently failed**: the reaper successfully cancelled the PEPE/USD stale order on the exchange and updated SQLite to `status='cancelled'`, but no `cc_memory` row with `category='stale_order_cancelled'` was written. SQLite query confirms zero rows. Codex's spec 27 implementation either skipped the memory write or used a different category name.

3. **Reaper-after-scheduler ordering**: `_reap_stale_cc_orders` is hooked at line 580 after `_handle_effects` at line 579, which is after `scheduler.run_cycle()` at line 564. If the scheduler raises, the reaper never runs. The reaper should run BEFORE the scheduler so it can clear stuck orders that may be blocking the scheduler's logic.

## Desired outcome

The bot is robust against:
- Newly-acquired CC API positions on startup (no deadlock)
- Stale orders that need clearing before the scheduler can succeed (reaper runs first)
- Missing price data on rotation tree roots (skip monitoring with a warning rather than aborting the cycle)

The reaper writes its memory entries correctly so the orchestrator can see the pattern.

## Acceptance criteria

1. **Move `_reap_stale_cc_orders` to BEFORE `scheduler.run_cycle()`** in `runtime_loop.py:run_once`. Specifically: it should run after `_ensure_websocket_connected` and `_ensure_subscriptions` (so the bot has a chance to subscribe to new pairs), but BEFORE the scheduler call. The new order in `run_once` should look approximately:
   ```
   await self._ensure_websocket_connected()
   await self._ensure_subscriptions()
   await self._reap_stale_cc_orders(now)   # <-- moved here
   try:
       state = ...
       new_state, effects = self._scheduler.run_cycle(state)
       ...
   except (ExchangeError, KrakenBotError) as exc:
       ...
   await self._maybe_bind_tree_to_position()
   ...
   ```
2. **Defensive missing-price handling**: in the `except (ExchangeError, KrakenBotError)` block at line 568, detect `MissingCurrentPriceError` specifically. When caught:
   - Log a warning with the missing pair
   - DO NOT abort the cycle entirely. Instead, attempt to continue with a one-shot REST price fetch for the missing pair, populate `current_prices`, and retry `scheduler.run_cycle()` once.
   - If the retry also fails, fall through to the existing error handling.
   - This is a defense-in-depth layer behind the emergency seeding patch from commit 66d72e4.
3. **Fix the spec 27 reaper memory write**: in `_reap_stale_cc_orders`, after the SQLite status update succeeds, write a `cc_memory` row with `category='stale_order_cancelled'`, `pair=order.pair`, `content={order_id, age_minutes, limit_price, side, base_qty}`, `importance=0.6`. Use the same memory-write API spec 24 uses for `reconciliation_anomaly` (it lives in `runtime_loop.py` already -- find the `_handle_effects` block where reconciliation_anomaly memories are written). If the call fails, log a warning but don't break the reaper.
4. **Keep the emergency commit's REST-seed logic** from `66d72e4`. Don't remove it -- it's a useful belt to the new suspenders.
5. Add tests in `tests/test_runtime_loop.py`:
   - `test_run_once_reaper_runs_before_scheduler`: instrument the order of calls, assert reaper is called before scheduler
   - `test_run_once_recovers_from_missing_price_error`: simulate a `MissingCurrentPriceError` from the scheduler, mock a REST fetch to populate the pair, assert the cycle eventually succeeds
   - `test_reaper_writes_stale_order_cancelled_memory`: cancel a stale CC order, assert a `cc_memory` row with `category='stale_order_cancelled'` exists. (This test is also in spec 27 but apparently didn't enforce -- make sure it actually fires the assertion against real DB state.)
6. Full pytest green.

## Non-goals

- Do not redesign the scheduler.
- Do not implement automatic WebSocket subscription per asset (the existing `_ensure_subscriptions` logic handles that, just needs to run earlier).
- Do not retroactively fix the missing memory rows from the PEPE cancellation today.
- Do not change `_collect_root_prices`. The emergency patch at startup is correct and should stay.

## Files in scope

- `runtime_loop.py`
- `tests/test_runtime_loop.py`
- `tasks/specs/28-startup-race-hardening.result.md`

## Evidence

- `state/scheduled-logs/main_restart_20260412_200608.log`: shows the deadlock cycles at 00:06:29, 00:06:59, 00:07:29 with identical `Missing current price for pair 'TRU/USD'` errors
- `state/scheduled-logs/main_restart_20260412_201317.log`: shows recovery after the emergency seed patch
- `data/bot.db orders WHERE pair='PEPE/USD'`: status now `cancelled` (reaper worked)
- `data/bot.db cc_memory WHERE category='stale_order_cancelled'`: zero rows (memory write missing)
