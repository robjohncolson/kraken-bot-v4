# Spec 27 -- Bot reaps stale CC API orders

## Problem

CC's brain placed `OJVNHM-NXESV-PBL5ZG` (PEPE/USD sell, 10.3M qty @ $0.000003512) at 21:58 UTC. The order is still `open` on Kraken at 22:59 UTC (~1 hour later) because the limit price ($3.512e-06) is ABOVE the current market price ($3.49e-06). Limit sells only fill at-or-above the limit, so the order will sit forever until PEPE bounces.

The brain noticed at 22:14 UTC and wanted to re-place at the new market price. But spec 12's pending-order chokepoint correctly blocked the duplicate proposal: `Pending orders blocking re-proposal: ['PEPE/USD']`.

Result: the order is stuck in a deadlock between the exchange (waiting for an unrealistic price) and the brain (blocked from updating). The bot needs a stale-order reaper.

## Desired outcome

The bot has a background reaper that, every reducer cycle (or every N seconds), scans for CC-API-placed orders that have been pending too long without filling. It cancels them on the exchange and marks them as cancelled in SQLite. The brain then naturally re-proposes on its next cycle with current market data.

## Acceptance criteria

1. New helper in `runtime_loop.py` (or wherever the existing reducer cycle hooks live) called `_reap_stale_cc_orders(now)`:
   - Queries SQLite: `SELECT order_id, pair, side, base_qty, limit_price, created_at FROM orders WHERE kind='cc_api' AND status='open' AND created_at < ? -- threshold`
   - Threshold: 15 minutes by default. Configurable via `Settings.cc_order_max_age_minutes` (default 15).
   - For each stale order, call `executor.execute_cancel(order_id)`. Catch `CancelOrderNotFoundError` (already filled/cancelled) and just mark as cancelled in SQLite without raising.
   - Update SQLite: `UPDATE orders SET status='cancelled' WHERE order_id=?` (or whatever existing status pattern matches).
   - Log: `"Reaper cancelled stale CC order %s on %s (age=%dm)"`
   - Write a `cc_memory` row with `category='stale_order_cancelled'`, `pair=order.pair`, `content={order_id, age_minutes, limit_price, side, base_qty}`, `importance=0.6` so the orchestrator can see the pattern.
2. Hook the reaper into the existing reducer loop. Find the equivalent of `_settle_rotation_fills` or `_handle_effects` and call `_reap_stale_cc_orders(now)` once per cycle, BEFORE the brain's next pending-order check would happen.
3. **Do NOT cancel** orders with `kind='rotation_entry'` or `kind='rotation_exit'` -- those are TP/SL waiting orders, they're SUPPOSED to sit and wait. Only `kind='cc_api'`.
4. **Do NOT cancel** orders with `created_at` newer than the threshold. Default 15 min.
5. **Idempotency**: if a reaper fires on an order that has already been cancelled, the second cancel returns gracefully (no exception).
6. Add a unit test in `tests/test_runtime_loop.py`:
   - `test_stale_cc_order_cancelled_after_threshold`: insert a fake order with kind='cc_api', status='open', created_at = 20 min ago. Run reaper. Assert executor.cancel was called and SQLite status updated.
   - `test_reaper_skips_fresh_cc_order`: same but created_at = 5 min ago. Assert no cancel.
   - `test_reaper_skips_rotation_entry_orders`: stale order with kind='rotation_entry'. Assert no cancel.
   - `test_reaper_handles_already_filled_order`: mock executor.execute_cancel to raise CancelOrderNotFoundError. Assert reaper continues (no propagation), SQLite updated to cancelled.
   - `test_reaper_writes_stale_order_memory`: assert a cc_memory row with category='stale_order_cancelled' is written for each cancelled order.
7. Full pytest green.

## Non-goals

- Do not cancel TP/SL orders. Those are intentional limit-wait orders.
- Do not auto-replace the cancelled order with a new one. The brain will re-propose naturally next cycle.
- Do not modify the brain's pending-order chokepoint (spec 12). It's correct as-is -- the chokepoint just needs to see no pending order, which the reaper provides by cancelling.
- Do not implement per-pair custom thresholds. Default 15 min for all CC orders.
- Do not address the case where the brain wants to place a NEW order while a stale one is being reaped (race condition). The next brain cycle handles it.

## Files in scope

- `runtime_loop.py`
- `core/config.py` (only if adding `cc_order_max_age_minutes` setting requires it -- check existing Settings dataclass first)
- `tests/test_runtime_loop.py`
- `tasks/specs/27-bot-stale-cc-order-reaper.result.md`

## Evidence

- Live PEPE/USD order via /api/open-orders: `OJVNHM-NXESV-PBL5ZG sell 10348231 @ 0.000003512 status=open opentm=1776031136`
- SQLite `orders` row: `('OJVNHM-NXESV-PBL5ZG', 'sell', '10348231.25', '0', '0.000003512', 'open', '2026-04-12 21:58:56')`
- Brain report `state/cc-reviews/brain_2026-04-12_2259.md`: `Pending orders blocking re-proposal: ['PEPE/USD']`
- Brain decision `cc_memory.category='decision'` at 22:14 UTC: brain wanted to re-sell at $3.49e-06 (matching market) but the chokepoint blocked it
