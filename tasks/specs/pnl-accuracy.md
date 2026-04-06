# Spec: P&L Tracking Accuracy for Root Exits

**Date**: 2026-04-06
**Priority**: 1
**Status**: Spec

## Motivation

The `trade_outcomes` table records P&L for completed trades, but the data is inaccurate for root node exits (pre-existing holdings the bot didn't originally purchase). Three bugs corrupt P&L reporting:

1. **Stale entry_cost**: Set at deadline evaluation time, never recalculated if `quantity_total` changes before exit. Real example: USD root recorded entry_cost=$36.96 but only exited $21.11 — phantom -$15.85 "loss."
2. **Missing opened_at**: Root nodes never have `opened_at` set, so `hold_hours` is always NULL and the opened_at fallback uses close time (making it look like 0h holds).
3. **No root/child distinction**: Aggregate queries (win rate, total P&L) mix inaccurate root P&L with accurate child round-trip P&L. No column to filter them apart.

## Design

### Fix 1: Recalculate entry_cost at root exit time

**File**: `runtime_loop.py`, function `_handle_root_expiry()` (~line 2147-2158)

**Current behavior**: The `update_node()` call before `_close_rotation_node()` sets `entry_pair`, `order_side`, `entry_price`, `exit_reason`, `ta_direction` — but does NOT recalculate `entry_cost`. The stale value from deadline evaluation persists.

**Fix**: Recalculate `entry_cost` using the node's current `quantity_total` and the OHLCV `last_close` price (already fetched at line 2149). For assets priced in USD, this gives the accurate USD value at exit time.

```python
# Line ~2149 (already exists)
last_close = Decimal(str(bars["close"].iloc[-1]))

# NEW: Recalculate entry_cost from current quantity and price
recalculated_entry_cost = node.quantity_total * last_close

# Line ~2150-2158 — add entry_cost to existing update_node call
self._rotation_tree = update_node(
    self._rotation_tree,
    node.node_id,
    entry_pair=pair,
    order_side=entry_side,
    entry_price=last_close,
    entry_cost=recalculated_entry_cost,  # NEW
    exit_reason="root_exit_" + direction,
    ta_direction=direction,
)
```

**Why last_close, not _root_usd_prices**: `last_close` is the OHLCV close price of the exit pair (e.g., ADX/USD), freshly fetched seconds ago. `_root_usd_prices` is a cache that may be stale. Using `last_close` ensures entry_cost matches the price environment at exit.

**Edge case — entry_side=SELL**: When `order_side=SELL` (root holds quote currency, e.g., USD root exiting via USDT/USD), the entry_cost should reflect the quote value. `quantity_total * last_close` still works because `quantity_total` is in the root's denomination (USD) and `last_close` is the pair price. However, for USD-denominated roots where the pair is X/USD, the entry_cost in USD is simply `quantity_total` (since the root IS USD). The `last_close` for USDT/USD is ~1.0, so the math is approximately correct. For non-USD quote roots this is also fine — `quantity_total * pair_price` gives a reasonable USD-equivalent value.

### Fix 2: Set opened_at when deadline is first assigned

**File**: `runtime_loop.py`, function `_evaluate_root_deadlines()` (~line 2034-2044)

**Current behavior**: The `update_node()` call that assigns the first deadline sets `deadline_at`, `window_hours`, `entry_pair`, `order_side`, `confidence`, `entry_cost`, `ta_direction` — but NOT `opened_at`.

**Fix**: Add `opened_at=now` to the `update_node()` call.

```python
# Line ~2034-2044
self._rotation_tree = update_node(
    self._rotation_tree,
    node.node_id,
    deadline_at=deadline,
    window_hours=window_hours,
    entry_pair=pair,
    order_side=entry_side,
    confidence=confidence,
    entry_cost=entry_cost,
    ta_direction=direction,
    opened_at=now,  # NEW — marks when bot started managing this root
)
```

**Why this is safe**: The guard at line 1988-1999 (`if node.deadline_at is not None: continue`) prevents this code from running again on roots that already have deadlines. So `opened_at` is set exactly once per root lifecycle. If a root expires and gets re-evaluated (deadline cleared → new deadline set), `opened_at` will be updated to the new evaluation time, which is correct — it's a new management window.

**Semantics**: For roots, `opened_at` means "when the bot started actively managing this holding" (not when the user originally purchased it). This is the meaningful timestamp for hold_hours.

### Fix 3: Add node_depth column to trade_outcomes

**File**: `persistence/sqlite.py`

#### 3a. Schema migration

Add to the existing `_migrate_columns()` pattern (around line 145-214):

```python
# In _migrate_columns(), add:
("trade_outcomes", "node_depth", "INTEGER DEFAULT 0"),
```

This uses the existing ALTER TABLE migration infrastructure. Existing rows get `node_depth=0` (root) by default, which is correct since all 5 existing trade_outcomes are root exits.

#### 3b. Update insert_trade_outcome()

**File**: `persistence/sqlite.py`, function `insert_trade_outcome()` (~line 537-585)

Add `node_depth: int = 0` parameter. Include in INSERT statement.

```python
def insert_trade_outcome(
    self,
    *,
    node_id: str,
    pair: str,
    direction: str,
    entry_price: Decimal | str,
    exit_price: Decimal | str,
    entry_cost: Decimal | str,
    exit_proceeds: Decimal | str,
    net_pnl: Decimal | str,
    fee_total: Decimal | str | None,
    exit_reason: str,
    hold_hours: float | None,
    confidence: float | None,
    opened_at: str,
    closed_at: str,
    node_depth: int = 0,  # NEW
) -> None:
```

#### 3c. Pass node_depth at call site

**File**: `runtime_loop.py`, `_settle_rotation_fills()` exit path (~line 1545-1564)

```python
self._writer.insert_trade_outcome(
    node_id=node.node_id,
    pair=node.entry_pair or "",
    direction=node.order_side.value,
    entry_price=entry_fill_price,
    exit_price=fill_price,
    entry_cost=entry_cost,
    exit_proceeds=proceeds,
    net_pnl=proceeds - entry_cost,
    fee_total=fill_fee,
    exit_reason=node.exit_reason or "unknown",
    hold_hours=hold_hours,
    confidence=node.confidence,
    opened_at=(...),
    closed_at=now.isoformat(),
    node_depth=node.depth,  # NEW — 0=root, 1+=child
)
```

## Affected Files

| File | Change |
|------|--------|
| `runtime_loop.py` | Fix 1: recalc entry_cost in `_handle_root_expiry()` (~L2150). Fix 2: add `opened_at=now` in `_evaluate_root_deadlines()` (~L2042). Fix 3c: pass `node_depth` in `_settle_rotation_fills()` (~L1545) |
| `persistence/sqlite.py` | Fix 3a: migration for `node_depth` column. Fix 3b: new param in `insert_trade_outcome()` |

## Test Plan

### New tests

1. **test_root_expiry_recalculates_entry_cost**: Create a root with stale entry_cost, trigger `_handle_root_expiry()` with known OHLCV bars, assert entry_cost on the updated node equals `quantity_total * last_close`.

2. **test_root_deadline_sets_opened_at**: Call `_evaluate_root_deadlines()` on a root with no deadline, assert `opened_at` is set to `now`. Call again — assert it doesn't overwrite (skipped by guard).

3. **test_trade_outcome_includes_node_depth**: Settle a root exit (depth=0) and a child exit (depth=1), assert `node_depth` column in trade_outcomes matches.

4. **test_node_depth_migration**: Open a DB with existing trade_outcomes (no node_depth column), run migration, assert column exists with default 0.

### Regression

- Full test suite: `python -m pytest` (624+ tests)
- Ruff: `python -m ruff check .`

## Success Criteria

- [ ] Root exits have entry_cost recalculated at exit time (no stale values)
- [ ] Root nodes have opened_at set when deadline is first assigned
- [ ] hold_hours is populated (not NULL) for root exits
- [ ] trade_outcomes has node_depth column (0=root, 1+=child)
- [ ] Existing 5 trade_outcomes retain node_depth=0 (migration default)
- [ ] All tests pass, ruff clean
- [ ] Aggregate P&L queries can filter: `WHERE node_depth > 0` for real round-trips
