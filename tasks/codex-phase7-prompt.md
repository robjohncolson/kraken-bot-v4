# Codex Prompt: EXPIRED Auto-Liquidation + Quote-Root P&L Fix (Phase 7)

**Repo**: kraken-bot-v4, branch `master`
**Context**: Bot is live, 628 tests. root-akt stuck EXPIRED with +$82 unrealized (no code path sells it). root-usd shows -$167 P&L (accounting artifact — deployed capital counted as loss).
Full spec at `tasks/specs/expired-auto-liquidation.md`.

---

## Phase 7A: Auto-Liquidate Exhausted EXPIRED Roots

### Task 1: Add RECOVERY_EXHAUSTED exit reason

**File**: `core/types.py` — search for `class RotationExitReason`

Add to the enum (after `RECONCILIATION`):
```python
RECOVERY_EXHAUSTED = "recovery_exhausted"
```

---

### Task 2: Force-close after recovery exhausted

**File**: `runtime_loop.py` — search for `if node.recovery_count >= 3:`

Replace the block (lines 1966-1973) that logs and `continue`s with:

```python
if node.recovery_count >= 3:
    logger.warning(
        "Root %s (%s) exhausted %d recovery attempts — force-closing",
        node.node_id,
        node.asset,
        node.recovery_count,
    )
    self._rotation_tree = update_node(
        self._rotation_tree,
        node.node_id,
        exit_reason=RotationExitReason.RECOVERY_EXHAUSTED.value,
    )
    await self._close_rotation_node(
        node, reason="recovery_exhausted", now=now,
    )
    continue
```

Ensure `RotationExitReason` is imported at the top of the file — search for `from core.types import` and add it if missing.

---

## Phase 7B: Quote-Asset Root P&L Shows Children Aggregate

### Task 3: Pre-compute children P&L

**File**: `runtime_loop.py` — search for `def _build_rotation_tree_snapshot`

After the `node_snaps_list: list[RotationNodeSnapshot] = []` line (line 2826), add a first pass over all nodes to build a children P&L lookup:

```python
# Pre-compute aggregate children P&L for quote-asset roots
children_pnl: dict[str, Decimal] = {}
for cn in tree.nodes:
    if cn.depth == 0 or cn.parent_node_id is None:
        continue
    cpnl = ZERO_DECIMAL
    if (
        cn.status == RotationNodeStatus.CLOSED
        and cn.exit_proceeds is not None
        and cn.entry_cost is not None
    ):
        cpnl = cn.exit_proceeds - cn.entry_cost
    elif cn.status in (RotationNodeStatus.OPEN, RotationNodeStatus.CLOSING):
        if (
            cn.entry_pair
            and cn.fill_price is not None
            and cn.entry_cost is not None
            and current_prices
        ):
            snap = current_prices.get(cn.entry_pair)
            if snap is not None:
                cp = snap.price if hasattr(snap, "price") else snap
                if cp is not None:
                    if cn.order_side == OrderSide.BUY:
                        cpnl = (cp - cn.fill_price) * cn.quantity_total
                    else:
                        cpnl = (cn.fill_price - cp) * cn.quantity_total
    if cpnl != ZERO_DECIMAL:
        children_pnl[cn.parent_node_id] = children_pnl.get(
            cn.parent_node_id, ZERO_DECIMAL
        ) + cpnl
```

### Task 4: Use children aggregate for quote-asset roots

**File**: `runtime_loop.py` — search for `current_value = n.quantity_total * usd_price` (inside the root P&L block, around line 2852)

Replace the entire root P&L elif block (lines 2840-2854):

```python
elif (
    n.depth == 0
    and n.status
    in (
        RotationNodeStatus.OPEN,
        RotationNodeStatus.CLOSING,
        RotationNodeStatus.EXPIRED,
    )
    and n.entry_cost is not None
):
    if n.asset in QUOTE_ASSETS:
        agg = children_pnl.get(n.node_id, ZERO_DECIMAL)
        realized_pnl = str(agg)
    else:
        usd_price = root_usd_prices.get(n.asset)
        if usd_price and usd_price > ZERO_DECIMAL:
            current_value = n.quantity_total * usd_price
            pnl = current_value - n.entry_cost
            realized_pnl = str(pnl)
```

**Note**: `QUOTE_ASSETS` is already imported at line 73. `OrderSide` is already imported. No new imports needed for this task.

---

## Tests

### Task 5: Phase 7A tests

Add tests (in `tests/test_runtime_loop.py` or new file `tests/test_recovery_exhausted.py`):

1. **`test_recovery_exhausted_calls_close`**: Create a root node with `recovery_count=3`, `status=EXPIRED`, `entry_pair="AKT/USD"`, `order_side=OrderSide.BUY`. Mock `_close_rotation_node`. Run the EXPIRED recovery code path. Assert `_close_rotation_node` was called with `reason="recovery_exhausted"`. Assert `exit_reason` was set to `"recovery_exhausted"` via `update_node`.

2. **`test_recovery_under_limit_resets_to_open`**: Root with `recovery_count=2`, `status=EXPIRED`. Assert node reset to OPEN with `deadline_at=None` and `recovery_count=3`. Existing behavior — this test may already exist; if so, verify it still passes.

3. **`test_recovery_exhausted_no_entry_pair`**: Root with `recovery_count=3`, `entry_pair=None`. Assert `_close_rotation_node` is called (it handles `entry_pair=None` gracefully by setting EXPIRED and returning).

### Task 6: Phase 7B tests

Add tests (in existing snapshot test file or new `tests/test_quote_root_pnl.py`):

4. **`test_quote_root_pnl_aggregates_children`**: Build a tree with a USD root (`entry_cost=Decimal("228")`) and two depth-1 children (`order_side=BUY`, `fill_price=Decimal("20")`, `quantity_total=Decimal("1")`, `status=OPEN`). Provide `current_prices` where child pair price is 30 and 15. Call `_build_rotation_tree_snapshot`. Assert USD root `realized_pnl` equals `str(Decimal("10") + Decimal("-5"))` = `"5"`.

5. **`test_quote_root_mixed_closed_open`**: USD root with one CLOSED child (`exit_proceeds=Decimal("110")`, `entry_cost=Decimal("100")`) and one OPEN child (unrealized -3). Assert root `realized_pnl == "7"`.

6. **`test_non_quote_root_pnl_unchanged`**: BTC root. Assert P&L uses `qty * price - entry_cost` (existing logic).

7. **`test_quote_root_no_children_zero`**: USD root with no children. Assert `realized_pnl == "0"`.

---

## What exists already

- `_close_rotation_node` (runtime_loop.py:2212) — handles all failure modes, no changes needed
- `update_node` (trading/rotation_tree.py) — already accepts `exit_reason` kwarg
- `QUOTE_ASSETS` (trading/pair_scanner.py:671) — frozenset of quote currencies, already imported in runtime_loop.py
- `RotationExitReason` (core/types.py:395) — enum to extend
- `OrderSide` (core/types.py) — already imported in runtime_loop.py

## Testing requirements

```bash
python -m pytest tests/ -x          # all tests pass (currently 628)
python -m ruff check .              # clean
```

Minimum 7 new test functions.

## Do NOT change

- `_close_rotation_node` logic — it already handles all edge cases correctly
- `_handle_root_expiry` — working correctly for normal (non-exhausted) expiry
- Root stop loss logic — correctly skips quote assets
- `evaluate_root_ta` in pair_scanner — root TA is separate
- `trade_outcomes` schema — no changes needed
- TUI rendering — it reads `realized_pnl` from snapshots, no TUI changes needed
