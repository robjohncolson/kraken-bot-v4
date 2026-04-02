# Scan Efficiency + Pre-Flight Balance Check

## Problem Statement

Two related waste sources:

1. **Scanner waste**: Each rotation scan fetches 20-150 OHLCV candles per asset with zero caching. Most pairs are filtered out AFTER the HTTP call. With 15 root nodes scanning every 5 minutes, the bot makes hundreds of unnecessary HTTP requests.

2. **Insufficient funds errors**: The bot places orders based on the rotation tree's shadow ledger (`node.quantity_total`), never checking actual Kraken balances. When the exchange rejects with `InsufficientFundsError`, the node is cancelled, the pair goes on 30-minute cooldown, and capital is wasted on a failed order.

The user's principle: **the bot should always know where the money is and what the minimum trade size is, so it should never run into insufficient funds.**

---

## Fix A: OHLCV Cache (Scan Efficiency)

### Motivation
`fetch_ohlcv()` in `exchange/ohlcv.py` makes a fresh HTTP request every time. The same pair gets scanned by multiple root nodes within the same cycle (e.g., ETH/USD is discovered from both root-USD and root-ETH). OHLCV data at 1-hour intervals doesn't change within 5 minutes.

### Solution
Add a module-level TTL cache in `exchange/ohlcv.py`:

```python
_ohlcv_cache: dict[str, tuple[float, list[dict]]] = {}  # pair → (expiry_monotonic, candles)
OHLCV_CACHE_TTL_SEC = 300  # 5 minutes

def fetch_ohlcv(pair, interval=60, count=50, timeout=10.0):
    cache_key = f"{pair}:{interval}"
    now = time.monotonic()
    cached = _ohlcv_cache.get(cache_key)
    if cached and now < cached[0] and len(cached[1]) >= count:
        return cached[1]
    # ... existing fetch logic ...
    _ohlcv_cache[cache_key] = (now + OHLCV_CACHE_TTL_SEC, candles)
    return candles
```

### Impact
- Same pair scanned by different roots within 5 min: 1 HTTP call instead of N
- 5-minute TTL matches plan_cycle interval — fresh data each cycle
- Zero behavior change for callers

### Affected files
- `exchange/ohlcv.py` — add cache dict + TTL check

---

## Fix B: Skip Underfunded Roots (Scan Efficiency)

### Motivation
Root nodes with tiny balances (e.g., root-BTC with 0.0006 BTC ≈ $50) still trigger a full pair scan. With max 3 children, each child gets ~$13 — often below ordermin. The scan is wasted because the planner's ordermin filter will reject all candidates anyway.

### Solution
In `runtime_loop.py::_maybe_run_rotation_planner()`, before calling `plan_cycle()`, compute the per-child budget for each root. If `root.quantity_free / max_children` is below a threshold (e.g., `min_position_usd * 1.5 = $15`), skip planning for that root entirely.

Actually, this is better placed inside `plan_cycle()` itself in `rotation_planner.py`, where the leaf iteration already happens. The existing check is:
```python
if leaf.quantity_free < Decimal(str(self._settings.min_position_usd)):
    continue
```

Change to account for per-child budget:
```python
per_child_budget = leaf.quantity_free / Decimal(str(max_children))
if per_child_budget < Decimal(str(self._settings.min_position_usd)):
    continue
```

### Impact
- Roots with $20 and 3 children max → $6.67 per child → skip (below $10 min)
- Eliminates scans that can never produce viable allocations
- Zero false negatives: if per-child budget is below min_position, `compute_child_allocations` would filter them all out anyway

### Affected files
- `trading/rotation_planner.py` — update leaf skip condition

---

## Fix C: Pre-Flight Balance Check (Critical)

### Motivation
`_execute_rotation_entries()` constructs an `OrderRequest` from the rotation tree node's `quantity_total` and `entry_price`, then sends it to the exchange without checking if the bot actually has enough of the source asset. The exchange rejects with `InsufficientFundsError`, which the bot catches and handles — but this is wasteful and the user correctly identifies it as a design hole.

### Root Cause
The rotation tree is a **shadow ledger**. `node.quantity_total` reflects what the planner allocated from `parent.quantity_free`, but:
1. Multiple roots can compete for the same exchange balance (e.g., root-USD children and root-USDT children both need USD-denominated assets)
2. Fees reduce actual available balance
3. Balance can be stale (up to 5 minutes between reconciles)

### Solution
Add a **pre-flight balance check** in `_execute_rotation_entries()` before placing each order:

```python
# Pre-flight: verify exchange has enough of the source asset
source_asset = node.from_asset or (
    node.entry_pair.split("/")[1] if node.order_side == OrderSide.BUY
    else node.entry_pair.split("/")[0]
)
available = _available_balance(self._state.kraken_state.balances, source_asset)
# Subtract already-committed pending orders for same asset
committed = sum(
    po.quote_qty if po.side == OrderSide.BUY else po.base_qty
    for po in self._state.bot_state.pending_orders
    if po.kind.startswith("rotation_") and _order_source_asset(po) == source_asset
)
effective_available = available - committed

order_cost = base_qty * node.entry_price if node.order_side == OrderSide.BUY else base_qty
if order_cost > effective_available:
    logger.info(
        "Skipping rotation entry %s: cost=%s > available=%s (%s committed)",
        node.node_id, order_cost, effective_available, committed,
    )
    # Cancel node and return capital — don't waste a cooldown slot
    self._rotation_tree = cancel_planned_node(self._rotation_tree, node.node_id)
    continue
```

### Key design decisions
1. **Use `kraken_state.balances`** — most recent reconciled balances (up to 5 min stale, but better than nothing)
2. **Subtract committed pending orders** — other rotation entries already in flight reduce available balance
3. **Cancel silently, no cooldown** — this isn't an exchange error, it's a planning overcommit. No need for 30-min pair cooldown.
4. **Log at INFO not WARNING** — this is expected pruning, not an error

### Why not just fetch fresh balances?
`fetch_kraken_state()` costs 4 REST rate-limit points. With 3+ entries per cycle, we'd burn 12+ points per cycle (Starter tier cap is 15). Use cached balances + committed tracking instead.

### Affected files
- `runtime_loop.py` — add pre-flight check in `_execute_rotation_entries()`
- Add helper `_available_balance()` and `_order_source_asset()`

---

## Dependency Graph

```
Fix A (OHLCV cache)      ─── independent
Fix B (skip underfunded)  ─── independent  
Fix C (pre-flight check)  ─── independent
```

All three are independent — can be implemented in parallel.

## Test Plan

- **Fix A**: Unit test: second call with same pair within TTL returns cached data (no HTTP). Call with different interval misses cache.
- **Fix B**: Unit test: leaf with $20 and max_children=3 is skipped. Leaf with $60 is scanned.
- **Fix C**: Unit test: entry skipped when order_cost > available - committed. Entry proceeds when sufficient. Verify no pair cooldown applied on pre-flight skip.
- **Regression**: All 560 existing tests pass.

## Success Criteria

- `InsufficientFundsError` never occurs in rotation entries (pre-flight catches it)
- OHLCV HTTP calls reduced ~50-70% via caching (same-cycle dedup)
- Underfunded roots never trigger pair scanning
- Zero functional change to trading behavior
