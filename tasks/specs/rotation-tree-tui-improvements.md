# Spec: Rotation Tree TUI Improvements

**Date**: 2026-04-03
**Status**: Spec

## Motivation

The rotation tree TUI (screen 7) has several display gaps:
1. Quote-currency roots (USD, EUR, GBP, USDT, USDC) show empty fields — skipped by `QUOTE_ASSETS` filter
2. Non-quote roots show no side or confidence despite having TA evaluation
3. Deadlines display in UTC, user wants Eastern Standard Time
4. No P&L shown for any roots (unrealized P&L needed)

**Design principle**: No currency gets special treatment. All assets are equal in the rotation tree.

## Change 1: Remove QUOTE_ASSETS Skip

### Current behavior
`_evaluate_root_deadlines()` at runtime_loop.py:1279 skips `node.asset in QUOTE_ASSETS`. USD, USDT, USDC, EUR, GBP, CAD, AUD, JPY, CHF never get deadlines.

### New behavior
Remove the skip. All roots get TA evaluation. For USD roots, `_find_root_exit_pair("USD")` finds pairs like BTC/USD where USD is the quote — returns `(pair="BTC/USD", entry_side=OrderSide.SELL)`. TA evaluation determines if crypto is bullish (USD should rotate out) or bearish (USD stays).

### Affected files
| File | Change |
|------|--------|
| `runtime_loop.py` | Remove `if node.asset in QUOTE_ASSETS: continue` in `_evaluate_root_deadlines()` (line ~1279) |

### Edge cases
- `_find_root_exit_pair("USD")` returns `("BTC/USD", SELL)` — USD is quote, so simulated entry is SELL (we "sold" BTC for USD). Exit reverses to BUY. This means "exiting USD" = buying crypto. Correct for the rotation model.
- If no pairs found for an asset, it's skipped with a debug log (existing behavior, unchanged)
- `QUOTE_ASSETS` constant kept for `_find_root_exit_pair` pair preference ordering (prefer USD > USDT > USDC as exit quote). Renamed conceptually: it's "preferred quote" not "skip list"

## Change 2: Set Confidence + Side on Roots

### Current behavior
`evaluate_root_ta()` returns `(direction, window_hours)`. `_evaluate_root_deadlines()` only sets `deadline_at`, `window_hours`, `entry_pair` on the root. No `confidence` or `order_side`.

### New behavior
1. `evaluate_root_ta()` returns 3-tuple: `(direction, window_hours, confidence)`
   - Confidence = `bullish_count / 3.0` for bullish direction
   - Confidence = `(3 - bullish_count) / 3.0` for bearish direction
   - Confidence = `1 / 3.0` for neutral (1 signal agrees, ambiguous)
2. `_evaluate_root_deadlines()` also sets `confidence` and `order_side` (from pair_info) via `update_node()`
3. `_handle_root_expiry()` unpacks 3-tuple, ignores confidence

### Affected files
| File | Change |
|------|--------|
| `trading/pair_scanner.py` | `evaluate_root_ta()` returns 3-tuple with confidence |
| `runtime_loop.py` | `_evaluate_root_deadlines()`: unpack 3-tuple, set `confidence` and `order_side` |
| `runtime_loop.py` | `_handle_root_expiry()`: unpack 3-tuple |

### API contract
```python
def evaluate_root_ta(bars: pd.DataFrame) -> tuple[str, float, float]:
    """Returns (direction, window_hours, confidence)."""
```

## Change 3: Deadlines in Eastern Time

### Current behavior
`tui/widgets/rotation_tree.py:91`: `node.deadline_at[:16]` — raw ISO truncation shows `2026-04-03T18:30` (UTC, no timezone indicator).

### New behavior
Parse ISO string, convert UTC to `America/New_York`, format as `MM/DD HH:MM ET`.

### Affected files
| File | Change |
|------|--------|
| `tui/widgets/rotation_tree.py` | New `_format_deadline_et()` helper, replace line 91 |

### Implementation
```python
from zoneinfo import ZoneInfo
_ET = ZoneInfo("America/New_York")

def _format_deadline_et(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    local = dt.astimezone(_ET)
    return local.strftime("%m/%d %H:%M ET")
```

## Change 4: Unrealized P&L on Roots

### Current behavior
P&L only computed for CLOSED child nodes with `entry_cost` and `exit_proceeds`. Roots never have `entry_cost`, always show "—".

### New behavior
1. Set `entry_cost` on roots when first evaluated: `entry_cost = quantity_total * usd_price`
2. In snapshot serialization, compute unrealized P&L for OPEN roots: `pnl = current_usd_value - entry_cost`
3. Reuse existing `realized_pnl` field on snapshot (works for both realized and unrealized)

### Affected files
| File | Change |
|------|--------|
| `runtime_loop.py` | In `_evaluate_root_deadlines()`: set `entry_cost` from prices_usd |
| `runtime_loop.py` | In snapshot building (~line 1959): compute unrealized P&L for OPEN roots |

### Edge cases
- Root USD price not available: skip entry_cost (leave None, P&L shows "—")
- Root is USD itself: `usd_price = 1.0`, so `entry_cost = quantity_total`. Current value also = quantity_total. P&L = 0.
- Price changes between cycles: unrealized P&L updates each cycle (expected, shows mark-to-market)

## Test Plan

1. `evaluate_root_ta()` returns 3-tuple with correct confidence values
2. All roots (including former QUOTE_ASSETS) get deadlines when bars are available
3. `_format_deadline_et()` converts UTC to Eastern correctly (including DST)
4. Root unrealized P&L computed correctly: `current_value - entry_cost`
5. Root with no price data shows "—" for P&L
6. Existing tests still pass (590 baseline)
