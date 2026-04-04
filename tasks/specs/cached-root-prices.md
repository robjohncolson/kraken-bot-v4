# Spec: Cached Root USD Prices for P&L

**Date**: 2026-04-03
**Status**: Spec

## Motivation

The snapshot builder needs USD prices for all root assets to compute unrealized P&L. Currently it relies on WebSocket prices (only has actively-subscribed pairs) plus hardcoded USDT/USDC=$1. Fiat currencies (EUR, GBP, CAD, AUD) are missing because USD/EUR etc. aren't in WebSocket subscriptions.

`_collect_root_prices()` already solves this with a REST OHLCV fallback, but calling it every 30s cycle in the snapshot builder would hammer Kraken's API.

## Design

Cache the result of `_collect_root_prices()` on the runtime instance with a TTL (5 minutes). The snapshot builder uses this cached map instead of building its own.

### Implementation

1. Add `_root_usd_prices: dict[str, Decimal]` and `_root_usd_prices_at: float` to `SchedulerRuntime.__init__`
2. Add `_refresh_root_usd_prices()` method: calls `_collect_root_prices()`, stores result + monotonic timestamp
3. Call `_refresh_root_usd_prices()` in `_evaluate_root_deadlines()` (already runs each cycle, already has access to prices)
4. Pass `self._root_usd_prices` to `_build_rotation_tree_snapshot()` via a new `root_usd_prices` parameter
5. Remove the inline price-building logic from `_build_rotation_tree_snapshot()` — just use the passed-in map

### TTL

5 minutes matches the OHLCV cache TTL. Prices don't need to be real-time for unrealized P&L display.

## Affected Files

| File | Change |
|------|--------|
| `runtime_loop.py` | Add cached price map on runtime, refresh in rotation cycle, pass to snapshot builder |

## Edge Cases

- First cycle before cache populated: `_root_usd_prices` starts as `{"USD": Decimal("1")}`, P&L shows dashes until first refresh
- REST failure for a fiat asset: price stays missing in cache, P&L shows dash for that root (graceful)
- Bot restart: cache rebuilds on first rotation cycle (~30s)

## Test Plan

1. Snapshot builder uses passed-in `root_usd_prices` for P&L computation
2. Fiat asset with price in map shows correct P&L
3. Asset missing from map shows no P&L (dash)
