# Spec 01 — Floor-round exit quantities

## Problem

The bot's exit loop is bricked: over 6 hours on 2026-04-12, 8 sell
attempts on CRV/COMP have all failed with `EOrder:Insufficient funds`,
and every subsequent 2h cycle re-proposes the same sell and re-fails.
Portfolio is stuck at $471.87, cannot cut losers in quality-collapse.

## Root cause

The bot rounds order quantities with `round(qty, N)` which is
**round-nearest**. Kraken reports available balance at 10 decimals of
precision; the bot was rounding at 6 decimals with round-nearest,
producing quantities that exceed the actual available balance by
nanoseconds:

| Asset | Actual available       | Bot sent     | Δ               |
|-------|------------------------|--------------|-----------------|
| CRV   | `91.5370539700`        | `91.537054`  | +0.0000000300   |
| COMP  | `1.9704579000`         | `1.970458`   | +0.0000001000   |

Both over by less than 1e-7, but enough for Kraken to reject.

## Desired outcome

Sell orders always specify a quantity **less than or equal to** the
actual available balance. Never round up. Never exceed what the
exchange says we own.

## Acceptance criteria

1. In `scripts/cc_brain.py`, any code path that builds a SELL order
   must round the quantity DOWN (floor) to the pair's lot_decimals,
   not round nearest.
2. The round-down must use `lot_decimals` from the authoritative
   Kraken `AssetPairs` response (already stored in the cached
   pair info by an earlier commit), not a hardcoded constant.
3. Fallback: if lot_decimals is unknown, floor to 6 decimals
   (current behavior was round-to-6).
4. A dry-run cycle that attempts to exit CRV logs a quantity
   strictly ≤ the actual available balance.
5. An actual (non-dry) cycle successfully places a sell order for
   at least one of the currently-stuck positions (CRV, COMP).

## Non-goals

- Do not modify BUY quantities (they're computed from USD value, not
  from existing holdings, so round-down isn't applicable).
- Do not change any pricing logic (pair_decimals).
- Do not touch the shadow veto or entry logic.
- Do not add new API endpoints.

## Evidence

`state/cc-reviews/brain_2026-04-12_1210.md` — latest failing cycle.
Look for `FAILED: CRV/USD — Exchange error: EOrder:Insufficient funds`.
