# Spec: EXPIRED Auto-Liquidation + Quote-Root P&L Fix (Phase 7)

**Date**: 2026-04-10
**Priority**: 1
**Status**: Spec

## Motivation

Two rotation tree issues discovered on the live bot:

1. **Stuck EXPIRED nodes**: root-akt has `recovery_count=3` and is permanently frozen in EXPIRED status with +$82 unrealized. After 3 recovery attempts (`runtime_loop.py:1966`), the code logs and `continue`s — no path ever sells the asset. The node and its capital are stranded.

2. **Misleading quote-root P&L**: root-usd shows -$167 P&L. The snapshot (`runtime_loop.py:2850-2854`) computes `qty_total * usd_price - entry_cost` for all roots. For USD, entry_cost was ~$228 at creation; capital deployed to children reduced qty to ~$61, so the P&L reads -$167. This is deployed capital, not a market loss. The root stop loss correctly skips quote assets (`runtime_loop.py:1617`), but the display is confusing.

These two fixes are independently deployable.

---

## Phase 7A: Auto-Liquidate Exhausted EXPIRED Roots

### Change 1: Add RECOVERY_EXHAUSTED exit reason

**File**: `core/types.py` — search for `class RotationExitReason`

Add to the enum:
```python
RECOVERY_EXHAUSTED = "recovery_exhausted"
```

### Change 2: Force-close after recovery exhausted

**File**: `runtime_loop.py` — search for `if node.recovery_count >= 3:`

Current code (lines 1966-1973):
```python
if node.recovery_count >= 3:
    logger.info(
        "Root %s (%s) exhausted %d recovery attempts — staying EXPIRED",
        node.node_id,
        node.asset,
        node.recovery_count,
    )
    continue
```

Replace with:
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

**Why this is safe**: `_close_rotation_node` (line 2212) already handles all failure modes:
- No `order_side` or `entry_pair` → sets EXPIRED, returns (line 2223)
- No valid price → sets EXPIRED, returns (line 2234)
- Zero balance → sets EXPIRED, returns
- Order placement failure → caught by existing try/except

If close fails (reverts to EXPIRED), next cycle retries (30s). AKT/USD is a valid pair — this resolves naturally.

---

## Phase 7B: Quote-Asset Root P&L Shows Children Aggregate

### Change 3: Two-pass P&L in snapshot builder

**File**: `runtime_loop.py` — search for `def _build_rotation_tree_snapshot`

**Step 1**: Before the main `for n in tree.nodes:` loop (after `node_snaps_list` initialization at line 2826), add a first pass to compute children P&L:

```python
# Pre-compute aggregate children P&L for quote-asset roots
children_pnl: dict[str, Decimal] = {}
for cn in tree.nodes:
    if cn.depth == 0 or cn.parent_node_id is None:
        continue
    cpnl = ZERO_DECIMAL
    if cn.status == RotationNodeStatus.CLOSED and cn.exit_proceeds is not None and cn.entry_cost is not None:
        cpnl = cn.exit_proceeds - cn.entry_cost
    elif cn.status in (RotationNodeStatus.OPEN, RotationNodeStatus.CLOSING):
        if cn.entry_pair and cn.fill_price is not None and cn.entry_cost is not None and current_prices:
            snap = current_prices.get(cn.entry_pair)
            if snap is not None:
                cp = snap.price if hasattr(snap, "price") else snap
                if cp is not None:
                    if cn.order_side == OrderSide.BUY:
                        cpnl = (cp - cn.fill_price) * cn.quantity_total
                    else:
                        cpnl = (cn.fill_price - cp) * cn.quantity_total
    if cpnl != ZERO_DECIMAL:
        children_pnl[cn.parent_node_id] = children_pnl.get(cn.parent_node_id, ZERO_DECIMAL) + cpnl
```

**Step 2**: In the root P&L block (lines 2840-2854), add a branch for quote-asset roots:

Current:
```python
elif (
    n.depth == 0
    and n.status in (RotationNodeStatus.OPEN, RotationNodeStatus.CLOSING, RotationNodeStatus.EXPIRED)
    and n.entry_cost is not None
):
    usd_price = root_usd_prices.get(n.asset)
    if usd_price and usd_price > ZERO_DECIMAL:
        current_value = n.quantity_total * usd_price
        pnl = current_value - n.entry_cost
        realized_pnl = str(pnl)
```

Replace with:
```python
elif (
    n.depth == 0
    and n.status in (RotationNodeStatus.OPEN, RotationNodeStatus.CLOSING, RotationNodeStatus.EXPIRED)
    and n.entry_cost is not None
):
    if n.asset in QUOTE_ASSETS:
        # Quote-asset roots: P&L = aggregate of children's P&L (deployed capital is not a loss)
        agg = children_pnl.get(n.node_id, ZERO_DECIMAL)
        realized_pnl = str(agg)
    else:
        usd_price = root_usd_prices.get(n.asset)
        if usd_price and usd_price > ZERO_DECIMAL:
            current_value = n.quantity_total * usd_price
            pnl = current_value - n.entry_cost
            realized_pnl = str(pnl)
```

**Note**: `QUOTE_ASSETS` is already imported at line 73 from `trading.pair_scanner`.

---

## Tests

### Phase 7A tests (add to `tests/test_runtime_loop.py` or new `tests/test_recovery_exhausted.py`):

1. **`test_recovery_exhausted_calls_close`**: Root with `recovery_count=3`, `status=EXPIRED`, valid `entry_pair="AKT/USD"`, `order_side=BUY`. Run the recovery loop. Assert `_close_rotation_node` was called. Assert `exit_reason == "recovery_exhausted"`.
2. **`test_recovery_under_limit_resets_to_open`**: Root with `recovery_count=2`. Assert node reset to OPEN with `deadline_at=None` (existing behavior unchanged).
3. **`test_recovery_exhausted_no_entry_pair`**: Root with `recovery_count=3` but `entry_pair=None`. Assert stays EXPIRED gracefully (no crash).

### Phase 7B tests (add to `tests/test_rotation_snapshot.py` or existing snapshot tests):

4. **`test_quote_root_pnl_aggregates_children`**: USD root (`entry_cost=228`) with two OPEN children (unrealized +10, -5). Assert root `realized_pnl == "5"`, not "-167".
5. **`test_quote_root_pnl_mixed_closed_open`**: USD root with one CLOSED child (proceeds=110, cost=100 → +10) and one OPEN child (unrealized -3). Assert root shows "+7".
6. **`test_non_quote_root_pnl_unchanged`**: BTC root with children. Assert P&L still uses `qty * price - entry_cost`.
7. **`test_quote_root_no_children_shows_zero`**: USD root with no children. Assert `realized_pnl == "0"`.

---

## Edge Cases

- **AKT no WebSocket**: `_close_rotation_node` uses `entry_price` as fallback (line 2233) — sell order gets a price
- **Close fails repeatedly**: Node stays EXPIRED, retries each 30s cycle. Pair is valid, will resolve
- **PLANNED children** (no `fill_price`): Skip in children_pnl — `fill_price is None` guard catches this
- **GBP root** (in QUOTE_ASSETS): GBP is quote-currency, so it would use children aggregate too. If GBP has no children, shows P&L=0. This is correct — GBP's price fluctuation vs USD is not meaningful for rotation P&L

## Success Criteria

- [ ] No EXPIRED node with `recovery_count >= 3` holds a nonzero balance indefinitely — bot places exit order
- [ ] `RECOVERY_EXHAUSTED` exit reason appears in `RotationExitReason` enum
- [ ] Quote-asset root P&L (USD, USDT, USDC, EUR, GBP, etc.) reflects children aggregate, not raw balance delta
- [ ] Non-quote root P&L calculation unchanged
- [ ] All existing tests pass; 7+ new tests added
- [ ] `ruff check .` clean
