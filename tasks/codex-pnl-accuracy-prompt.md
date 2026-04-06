# Codex Prompt: P&L Tracking Accuracy (Phase 5)

**Repo**: kraken-bot-v4, branch `master`
**Context**: Bot is live and trading. 5 trade_outcomes recorded but all show inaccurate P&L for root exits. USD root has phantom -$15.85 loss from stale entry_cost. hold_hours is always NULL. No way to separate root exits from child round-trips.
Full spec at `tasks/specs/pnl-accuracy.md`.

---

## Task 1: Recalculate entry_cost at root exit time

**File**: `runtime_loop.py`, function `_handle_root_expiry()` — search for `async def _handle_root_expiry`

**Current** (around line 2147-2158): The `update_node()` call before `_close_rotation_node()` sets `entry_pair`, `order_side`, `entry_price`, `exit_reason`, `ta_direction` but does NOT update `entry_cost`. The value from deadline evaluation (hours/days earlier) persists, even if `quantity_total` has changed.

**Fix**: After the existing `last_close = Decimal(str(bars["close"].iloc[-1]))` line, compute `recalculated_entry_cost = node.quantity_total * last_close`. Add `entry_cost=recalculated_entry_cost` to the `update_node()` call that already exists right below it.

That's it — one new line computing the value, one new kwarg in the existing call.

**Tests** (add to `tests/test_root_exit_windows.py` or a new `tests/test_pnl_accuracy.py`):
- Create a root node with `entry_cost=Decimal("100")` and `quantity_total=Decimal("50")`. Mock OHLCV bars with `close=Decimal("1.5")`. Trigger `_handle_root_expiry()`. Assert the node's entry_cost is now `50 * 1.5 = 75`, not 100.
- Same test but with `order_side=SELL` (quote-currency root). Assert entry_cost is still `quantity_total * last_close`.

---

## Task 2: Set opened_at when root deadline is first assigned

**File**: `runtime_loop.py`, function `_evaluate_root_deadlines()` — search for `async def _evaluate_root_deadlines`

**Current** (around line 2034-2044): The `update_node()` call that assigns the first deadline sets `deadline_at`, `window_hours`, `entry_pair`, `order_side`, `confidence`, `entry_cost`, `ta_direction`. Does NOT set `opened_at`.

**Fix**: Add `opened_at=now` to that `update_node()` call. The `now` parameter is already available in scope (it's a function parameter).

**Why safe**: The guard at line ~1988-1999 (`if node.deadline_at is not None: continue`) prevents this from running on roots that already have deadlines. So `opened_at` is set exactly once.

**Tests**:
- Call `_evaluate_root_deadlines()` on a fresh root (no deadline). Assert `opened_at` equals `now`.
- Call again on the same root (now has deadline). Assert `opened_at` is unchanged (skipped by guard).
- Trigger a full root lifecycle (deadline → expiry → exit fill). Assert `hold_hours` in trade_outcomes is a positive float, not NULL.

---

## Task 3: Add node_depth to trade_outcomes

### 3a. Schema migration

**File**: `persistence/sqlite.py` — search for `_migrate_columns`

**Current**: There's a list of `(table, column, type_default)` tuples that drive ALTER TABLE migrations.

**Fix**: Add one entry: `("trade_outcomes", "node_depth", "INTEGER DEFAULT 0")`. Existing rows get 0 (root) by default, which is correct — all 5 current records are root exits.

### 3b. Update insert_trade_outcome()

**File**: `persistence/sqlite.py` — search for `def insert_trade_outcome`

**Fix**: Add `node_depth: int = 0` parameter. Add it to the INSERT column list and VALUES tuple. Match the existing pattern of the other columns.

### 3c. Pass node_depth at call site

**File**: `runtime_loop.py`, function `_settle_rotation_fills()` — search for `insert_trade_outcome`

**Current** (around line 1545-1564): The call passes 14 keyword arguments.

**Fix**: Add `node_depth=node.depth` as the 15th argument. `node.depth` is already available on the RotationNode (0 for roots, 1+ for children).

**Tests**:
- Open a fresh DB, insert a trade_outcome with `node_depth=2`. Read it back, assert `node_depth=2`.
- Open a DB with an existing trade_outcome (pre-migration, no node_depth column). Run `_migrate_columns()`. Assert column exists and value is 0.
- Integration: settle a root exit (depth=0) and a child exit (depth=1). Query `SELECT node_depth FROM trade_outcomes`. Assert [0, 1].

---

## What exists already

- `update_node()` in `trading/rotation_tree.py` — immutable node update via `dataclasses.replace()`
- `_migrate_columns()` in `persistence/sqlite.py` — existing ALTER TABLE migration infrastructure
- `RotationNode.depth` field in `core/types.py` — already available, 0 for roots, 1+ for children
- `RotationNode.opened_at` field in `core/types.py` — already exists, just never set for roots

## Testing requirements

```bash
python -m pytest                    # all tests pass (currently 624)
python -m ruff check .              # clean
```

New tests should cover:
1. entry_cost recalculation on root expiry
2. opened_at set on deadline assignment
3. node_depth in trade_outcomes (insert, migration, round-trip)

Minimum 4 new test functions.

## Do NOT change

- `build_root_nodes()` in `trading/rotation_tree.py` — roots are intentionally created without opened_at/entry_cost (those are set later by runtime_loop)
- `make_child_node()` in `trading/rotation_tree.py` — child nodes already set opened_at correctly
- Entry fill path in `_settle_rotation_fills()` (the `rotation_entry` branch) — child entry_cost is already accurate
- `exit_proceeds()` or `exit_base_quantity()` — these are correct
- The `RotationNode` dataclass fields — no new fields needed
