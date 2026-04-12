# Spec 08 — Maker-fee optimization (critical for P&L)

## Problem

Every trade is paying **taker fees (~0.25% per side)** instead of
**maker fees (0.16% per side)** because the bot places "limit" orders at
`mid * 1.002` for buys and `mid * 0.998` for sells. A 0.2% buffer
above/below mid is large enough to cross the spread on any liquid pair,
so the order immediately executes as a taker.

Real fee observations from 7-day trade outcomes:

| Pair | Cost | Fee | Fee rate | Per side |
|------|------|-----|----------|----------|
| AVAX/USD | $82.72 | $0.33 | 0.40% | 0.20% |
| ATOM/USD | $81.94 | $0.33 | 0.40% | 0.20% |
| AERO/USD | $38.48 | $0.10 | 0.25% | 0.125% |
| TON/USD  | $20.67 | $0.08 | 0.40% | 0.20% |
| APU/USD  | $41.78 | $0.17 | 0.40% | 0.20% |

Most trades pay 0.40% roundtrip (0.20% per side). Kraken's public fee
schedule says taker at lowest tier is 0.25%, maker is 0.16%. The
observed ~0.20% is consistent with one side crossing and the other
resting (a rare case) or mixed maker/taker fills, but most trades are
effectively taker.

## Why this matters

Current 7-day stats:
- Gross wins: +$2.11
- Gross losses (ex outlier): −$0.83
- Total fees: −$1.77
- Net (ex outlier): −$0.49 (essentially break-even)

The bot's P&L is **bottlenecked by fees, not signal quality**. Reducing
the effective fee rate from 0.40% → 0.32% roundtrip (pure maker) would
have turned the 7-day period from −$0.49 to roughly +$0.38 (ex outlier).
Over weeks/months, the compound effect is multiples of the 1%/month
target.

## Desired outcome

Orders are placed at passive limit prices (at or inside the best
bid/ask without crossing), so they rest on the book and execute as
maker fills. Accept that some orders won't fill within the cycle window;
the pending-order blocklist already prevents re-proposing them.

## Acceptance criteria

1. A new constant `ENTRY_PRICE_BUFFER_BPS` (basis points from mid) and
   `EXIT_PRICE_BUFFER_BPS` replace the hardcoded 1.002 / 0.998 factors.
2. Default values: **10 bps (0.10%)** for buys above mid, **10 bps (0.10%)**
   for sells below mid. Much tighter than the current 0.2% so fewer
   trades cross, and where they do the effective taker rate is lower.
3. All four call sites updated:
   - `sweep_dust` sell (line ~1010)
   - Step 5a rotation (line ~1236)
   - Step 5b entry (line ~1261)
   - Step 5c exit (line ~1278)
4. A log line at order-build time shows the computed limit price and
   notes whether it is expected to fill as maker or taker (rough
   heuristic: if the buffer exceeds the typical spread for the pair,
   it will likely cross).
5. After the fix lands, ~5 real cycles worth of fills are observed and
   their average fee rate is computed. Target: average roundtrip fee
   ≤ 0.36% (mid-way between current 0.40% and pure maker 0.32%).
6. If after 5 cycles the fill rate drops to zero (all passive orders
   expiring unfilled), the tuning needs another iteration — possibly
   with a "post-only" flag instead of a buffer.

## Non-goals

- Do not add a `postOnly` flag to the Kraken API call yet. Kraken
  supports it, but wiring it through the bot adapter is a bigger change.
  The tight-buffer approach is a first step that should work without
  adapter changes.
- Do not change the signal/scoring logic. This is purely order-placement
  mechanics.
- Do not change position sizing.
- Do not touch `check_pending_orders` cancellation timing — if an order
  rests unfilled for 2h it still gets cancelled.

## Evidence

- `/api/trade-outcomes?lookback_days=7` — shows 0.40% average roundtrip
  on most trades
- `scripts/cc_brain.py` lines 1010, 1236, 1261, 1278 — all four call
  sites use `* 1.002` or `* 0.998`
- Kraken fee schedule (public): 0.25% taker / 0.16% maker at tier 0
