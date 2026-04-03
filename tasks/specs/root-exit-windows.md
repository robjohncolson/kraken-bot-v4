# Spec: Root Exit Windows

**Date**: 2026-04-03
**Priority**: 1 (highest)
**Status**: Spec

## Motivation

Root nodes currently have `deadline_at=None` — they hold forever. This causes:
1. **Orphaned assets** from missed fills sit as permanent roots with no exit plan
2. **Portfolio fragmentation** — small bearish holdings never consolidate back to USD
3. **No exits ever** — roots never sell, only spawn children

## Design (Option B from CONTINUATION_PROMPT)

### Lifecycle

1. **On startup and every plan_cycle**: run TA on each root asset against its best quote pair
2. **Estimate window**: `hours_to_tp = tp_pct / hourly_volatility` (same formula as children, clamped 2-48h)
3. **Set deadline on root**: `root.deadline_at = now + timedelta(hours=hours_to_tp)`
4. **On deadline expiry — RE-EVALUATE, not hard sell**:
   - Re-run TA on the root asset
   - If BEARISH or NEUTRAL → sell to USD (or best available quote currency)
   - If still BULLISH → extend deadline with new estimate
5. **Root exit mechanics**: same as child exit — LIMIT sell, escalate to MARKET after 5min timeout
6. **Proceeds**: root sells → USD/quote received → new root node created for the proceeds

### Key constraint
This turns all holdings into actively-managed positions. No permanent "buy and hold."

## Affected Files

| File | Change |
|------|--------|
| `runtime_loop.py` | New `_evaluate_root_deadlines()` method called each cycle; root expiry handling in `_handle_rotation_expiry()` |
| `trading/rotation_tree.py` | New `set_root_deadline()` helper; modify `expired_nodes()` to include roots |
| `trading/pair_scanner.py` | Extract `_estimate_rotation_window_hours()` and TA classification for reuse on roots |
| `core/types.py` | No change — `deadline_at` field already exists on RotationNode |
| `persistence/sqlite.py` | No schema change — `deadline_at` column already exists in rotation_nodes |

## API / Function Contracts

### New: `evaluate_root_node(root: RotationNode, bars: pd.DataFrame) -> tuple[str, float]`
- **Location**: `trading/pair_scanner.py` (or new helper)
- **Returns**: `(direction: "bullish"|"bearish"|"neutral", window_hours: float)`
- **Uses**: Same EMA/RSI/MACD signals as `_scan_rotation_pair()`

### New: `set_root_deadline(tree: RotationTreeState, node_id: str, deadline: datetime) -> RotationTreeState`
- **Location**: `trading/rotation_tree.py`
- **Returns**: Updated tree with root's deadline_at set

### Modified: `_handle_rotation_expiry(now)` in `runtime_loop.py`
- Currently skips `depth == 0` nodes
- Must now handle root expiry: re-evaluate TA, sell if bearish/neutral, extend if bullish

### New: `_evaluate_root_deadlines(now)` in `runtime_loop.py`
- Called each plan cycle
- For each root with `deadline_at is None` or stale: fetch OHLCV, run TA, set deadline
- For roots hitting deadline: re-evaluate and act

## Edge Cases

1. **Root has no tradeable pair**: Some assets may not have a direct USD/USDT pair. Must find best quote currency (USD > USDT > USDC > EUR).
2. **Root below ordermin**: If root's value is below ordermin for its best pair, mark as dust (skip evaluation, log warning).
3. **USD/stablecoin roots**: USD, USDT, USDC should never get exit windows — they ARE the quote currency. Skip them.
4. **Concurrent exits**: If a root is already CLOSING (has pending exit order), don't re-evaluate.
5. **OHLCV fetch failure**: If bars can't be fetched for a root's pair, keep existing deadline (don't clear it).
6. **Re-evaluation extends indefinitely**: Bullish assets keep extending. This is by design — the alternative (forced sell) is worse.

## Test Plan

1. Unit: `set_root_deadline()` sets deadline correctly on root nodes
2. Unit: `evaluate_root_node()` returns correct direction/window for bullish/bearish/neutral bars
3. Unit: Root expiry triggers re-evaluation, not immediate sell
4. Unit: Bearish re-evaluation triggers exit order placement
5. Unit: Bullish re-evaluation extends deadline
6. Unit: USD/stablecoin roots are skipped
7. Unit: Dust roots (below ordermin) are skipped
8. Unit: Already-CLOSING roots are skipped
9. Integration: Full cycle — root gets deadline → expires → re-evaluates → sells or extends
