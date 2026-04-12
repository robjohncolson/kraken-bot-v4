# Plan — Spec 11: runtime_loop root-exit unit mixing fix

## Context for the implementer

Read first:
- `tasks/specs/11-runtime-loop-root-exit-unit-fix.spec.md`
- `tasks/specs/09-usdt-loss-investigation.result.md` (full diagnosis)

## Root cause (verified by reading code)

Three things combine to produce the bug:

1. `_find_root_exit_pair(asset)` at `runtime_loop.py:1975` returns
   `(pair, entry_side)` where `entry_side` is the **simulated entry**
   side. For `asset=USD`, the function picks `USDT/USD` and returns
   `OrderSide.SELL` (line 2000) because USD is the **quote**.

2. `_handle_root_expiry()` at `runtime_loop.py:2135` writes that
   simulated `entry_side` onto `node.order_side`, then calls
   `_close_rotation_node()` which reverses it (`exit_side = BUY`,
   `runtime_loop.py:2321`) and places a real BUY of `USDT/USD`.

3. When the fill arrives, `_settle_rotation_fills()` at
   `runtime_loop.py:1496` calls
   `proceeds = exit_proceeds(node.order_side, fill_qty, fill_price)`
   at line 1588. `exit_proceeds` (in `trading/rotation_tree.py:222`)
   for `entry_side=SELL` returns **`fill_qty`** under the assumption
   "parent denomination is base". But the real parent (the USD root)
   is denominated in **USD = the quote** of `USDT/USD`. The
   simulated-entry framing only works for normal child rotations
   where the parent denomination matches base/quote convention; for
   synthetic root exits where the asset is the quote, the framing
   inverts.

The same bug also affects `_handle_root_expiry`'s entry_cost
recalculation (line 2231):
`recalculated_entry_cost = node.quantity_total * last_close`.
That sets `entry_cost` to the full planned exit value in USD, but
when only a partial fill arrives, the row's `entry_cost` doesn't
match the row's `exit_proceeds` quantity.

## Implementation approach

**Recommended option (cleanest):** add an explicit "root_exit" code
path in `_settle_rotation_fills()` and `_handle_root_expiry()` that
does NOT use `exit_proceeds()`.

### Step 1 — fix `_settle_rotation_fills` for root exits

In the `elif kind == "rotation_exit":` branch starting at
`runtime_loop.py:1585`:

- Detect root exits: `if node.depth == 0:` (root nodes are depth-0)
- For root exits, compute proceeds in USD directly:
  - If `node.entry_pair` ends with `/USD` and the root asset is the
    quote: USD proceeds = `fill_qty * fill_price` (BUYing base, USD
    spent equals USD value of acquired base)
  - If `node.entry_pair` ends with `/USD` and the root asset is the
    base: USD proceeds = `fill_qty * fill_price` (SELLing base, USD
    received)
  - General case: `fill_qty * fill_price` is always the USD-equivalent
    when one side of the pair is USD
- For non-root exits (`node.depth > 0`), keep the existing
  `exit_proceeds()` call — it's correct for child rotations.

### Step 2 — fix `entry_cost` to match the actual fill

For root exits, set `entry_cost` based on the **actual fill value**,
not the planned full exit. Either:
- Recompute `entry_cost = fill_qty * fill_price` (treats round-trip
  as zero P&L for stablecoins, which is what we want)
- OR use the historical USD basis from `node.entry_cost` if it was
  set at original entry time (cleaner but requires verifying that
  field survives through `_handle_root_expiry`)

The pragmatic fix: when writing `trade_outcomes` for a root exit,
override `entry_cost` to `min(node.entry_cost, fill_qty * fill_price)`
when the row would otherwise be negative on a stablecoin.
**Better**: just use the actual fill cost basis for root exits and
let the row report ~$0 P&L on stablecoin parity trades.

### Step 3 — backfill / flag the existing bad row

Per spec acceptance criterion 5, do NOT silently rewrite the
historical `trade_outcomes.id=1` row. Either:
- Add an `anomaly_flag TEXT` column to `trade_outcomes` (migration
  in `persistence/sqlite.py`) and set it on rows where unit mixing
  is detected
- OR add a new helper view `trade_outcomes_clean` that filters
  flagged rows
- The spec 06 backfill filter and the brain's self-tune code already
  drop flagged outliers, so once the column exists no other code
  needs to change

### Step 4 — regression test

Add `tests/test_runtime_loop_root_exit.py`:

- Construct a fake USD root with `quantity_total = 36.96` and
  `entry_cost = 36.96`
- Simulate `_handle_root_expiry()` picking `USDT/USD` with
  `entry_side = SELL`
- Simulate the fill: `fill_qty = 21.11138898 USDT`,
  `fill_price = 0.99965`
- Assert: written `trade_outcomes` row has
  `entry_cost ≈ 21.10` AND `exit_proceeds ≈ 21.10` AND
  `abs(net_pnl) < 0.10`
- Add a property test: for any pair in `STABLECOINS`, simulated
  fills at parity must produce `abs(net_pnl) < 0.10 * entry_cost`

## Files to modify

- `runtime_loop.py` — `_settle_rotation_fills`, `_handle_root_expiry`
- `persistence/sqlite.py` — optional schema migration for anomaly_flag
- `tests/test_runtime_loop_root_exit.py` — new file
- Optionally `trading/rotation_tree.py` — add a docstring warning to
  `exit_proceeds()` that it does not handle synthetic root exits

## Validation

After implementation:

1. `python -m pytest tests/test_runtime_loop_root_exit.py -x`
2. `python -m pytest tests/ -x` — full suite still passes (679+)
3. Manual SQLite check: `SELECT * FROM trade_outcomes WHERE id=1` —
   row exists, anomaly_flag set (or row is corrected per chosen path)
4. Run `scripts/cc_brain.py --dry-run` — confirm 7-day P&L summary
   no longer shows `−$15.85` USDT loss after fix lands

## Dependencies

None (this is independent of specs 12 and 13).

## Risk

LOW. The change is scoped to root exits, which only fire on root
node expiry. Normal child rotations are unchanged. The historical
row stays intact (just flagged). No existing tests should break.
