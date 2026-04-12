# Spec 16 -- Persist CC API order placements to SQLite

## Problem

`/api/orders` POST in `web/routes.py:place_order()` (lines 307-332) places orders via `executor.execute_order()` and logs `"CC placed order ..."`, but it does **NOT** persist the order to SQLite. The runtime-managed order path in `runtime_loop.py:986-1008` and `runtime_loop.py:2395-2407` always calls `self._writer.upsert_order(...)` after a successful place.

The result, documented in `tasks/specs/15-untracked-assets-investigation.result.md`:

- The reconciler considers an asset "tracked" when its symbol appears in any persisted `orders.pair` or `positions.pair` row in SQLite
- Assets acquired through CC's API path (MON, HYPE, ALEO, etc.) never get an `orders` row, so they show up as "untracked" in the reconciler
- Result: `untracked_assets=4-5` warning fires every reducer cycle, polluting brain reports and triggering false reconciliation alarms

## Desired outcome

`/api/orders` placements are persisted to SQLite the same way the runtime-managed path persists them, so:

1. Assets acquired via CC stop appearing as "untracked"
2. The reconciler can correctly distinguish "untracked stranger asset" (real anomaly) from "asset CC bought intentionally"
3. The fill settlement / reconciliation paths can find the order record on subsequent cycles

## Acceptance criteria

1. `web/routes.py:place_order()` calls `SqliteWriter.upsert_order()` (or equivalent) immediately after `executor.execute_order()` returns successfully, with the same field set as `runtime_loop.py:_close_rotation_node()`'s `upsert_order` call:
   - `order_id` (txid)
   - `pair`
   - `client_order_id` (must be generated -- use `f"kbv4-cc-{txid}"` or similar)
   - `kind` (`"cc_api"` is a new kind value distinguishing this from `rotation_entry`/`rotation_exit`)
   - `side`
   - `base_qty`
   - `filled_qty=ZERO_DECIMAL`
   - `quote_qty=ZERO_DECIMAL`
   - `limit_price`
   - `exchange_order_id=txid`
   - `rotation_node_id=None`
2. The new `kind="cc_api"` is added to whatever enum or string set the `kind` column accepts. Search for existing `kind` values via `grep -rn '"rotation_entry"\|"rotation_exit"' --include="*.py" .`.
3. The handler still returns the same `{"txid": ..., "status": "placed", "pair": ...}` response shape -- no breaking change to CC's brain client.
4. If the SQLite write fails, log a warning but DO NOT fail the order placement (the order is already at Kraken). Return success with a `"warning": "..."` field in the response.
5. A regression test in `tests/web/test_routes.py` (or wherever the existing `/api/orders` tests live):
   - Mocks the executor to return a fake txid
   - Mocks the writer to verify `upsert_order` was called with the right kwargs
   - Asserts the response shape is unchanged
6. After the fix, the `untracked_assets` count for assets acquired via CC API should drop. (Cannot fully verify without restarting the bot and waiting for a brain cycle to place an order, but the test from criterion 5 is the proxy.)

## Non-goals

- Do not retroactively backfill historical CC API placements that were never persisted. Going forward only.
- Do not change the reconciler logic. The reconciler is correct given its inputs (per spec 15 finding).
- Do not address the residual orphan balances `FLOW` and `TRIA` that were acquired pre-tracking. Those are a separate concern (tolerated residuals or manual cleanup).
- Do not modify `cancel_order` -- only `place_order`.
- Do not change `executor.execute_order()` itself.

## Files in scope

- `web/routes.py` (the `place_order` handler)
- `tests/web/test_routes.py` (or the relevant test file)

## Evidence

- `tasks/specs/15-untracked-assets-investigation.result.md` (full diagnosis)
- `web/routes.py:307-332` (the bug)
- `runtime_loop.py:986-1008` and `runtime_loop.py:2395-2407` (the correct pattern to copy)
- `state/cc-reviews/brain_2026-04-12_*.md` (brain reports showing the recurring `untracked_assets=4-5` warning)
