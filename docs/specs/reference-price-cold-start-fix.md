# Reference Price Cold-Start Fix

## Problem

The bot generates beliefs (DOGE/USD bearish, conf=0.11) but cannot act because `reference_prices` is empty. The reducer logs: `belief_update: no reference price for DOGE/USD`.

## Root Cause

Chicken-and-egg timing in `runtime_loop.py`:

```
1. _ensure_subscriptions()  ← called first, but active_pairs is EMPTY (no beliefs yet)
2. _maybe_poll_beliefs()    ← generates beliefs, adds to state
3. Next cycle: _ensure_subscriptions() finally sees pairs, subscribes
4. Price ticks start flowing
5. reference_prices populated
6. Reducer can now act on beliefs
```

At cold start, `_active_pairs()` derives pairs from positions/beliefs/orders — all empty. So the WebSocket subscribes to nothing. Beliefs arrive in the same cycle but AFTER the subscription check.

## Fix

**In `_ensure_subscriptions()` (`runtime_loop.py`)**: include `allowed_pairs` from settings in the subscription set, not just pairs derived from state. This ensures DOGE/USD gets a WebSocket ticker subscription from the first cycle, regardless of whether beliefs/positions exist yet.

```python
async def _ensure_subscriptions(self) -> None:
    # Include allowed_pairs so we get price ticks from cold start
    active_pairs = sorted(
        _active_pairs(self._state) | self._settings.allowed_pairs
    )
    ...
```

**Scope**: ~3 lines changed in `runtime_loop.py`. No other files affected.

**Verification**:
- Bot logs should show `subscribe ticker: ['DOGE/USD']` at startup
- Price ticks should appear within seconds of WS connection
- `belief_update` should progress past the reference price check
- On bearish signal, reducer should attempt spot sell of DOGE inventory

## Sizing

| Step | Effort | Files |
|------|--------|-------|
| Fix `_ensure_subscriptions` | 3 lines | `runtime_loop.py` |
| Verify WS subscribes at startup | smoke test | — |
| Verify belief→order flow | smoke test | — |

Total: ~15 minutes implementation + verification.
