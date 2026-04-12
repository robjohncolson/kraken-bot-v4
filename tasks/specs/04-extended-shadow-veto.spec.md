# Spec 04 — Extended shadow veto: non-USD picks

## Problem

The current shadow veto is narrow: it only blocks new cash-to-crypto
entries when shadow's best hold is literally `USD`. Over the last 24
hours of live operation, shadow's best pick has been BTC (55%) or ETH
(45%) — never USD — so the veto has not fired once. Meanwhile, live
deployed USD into CRV, RENDER, COMP, AUD, WIF, HYPE, SUI, APE — and
every one of those positions subsequently collapsed.

Shadow was right and live was wrong on 11/11 cycles, but shadow had
no authority over anything except the USD case.

## Desired outcome

When shadow has a strong, high-confidence pick for "best asset to
hold" and that pick is **different** from live's entry target,
shadow gets to either block the entry or redirect it toward its own
pick — instead of passively letting live deploy into the losing alt.

## Design options

### Option A — Hard veto (conservative)

If shadow's best hold is eligible (n ≥ 3) and top3_mean ≥ 0.65, and
the best asset ≠ live's entry target, BLOCK the entry. Bot holds
cash. Next cycle re-evaluates.

Pros: simple, proven mechanism (same as USD veto)
Cons: bot doesn't deploy capital into shadow's winning pick either;
USD accumulates; might overshoot into paralysis

### Option B — Target redirect (assertive)

If shadow's best hold is eligible with top3_mean ≥ 0.65, ignore
live's entry target and construct a new entry targeting shadow's
pick. Requires finding a tradeable pair for shadow's pick (usually
`{asset}/USD`) and confirming it clears the `MIN_REGIME_GATE`.

Pros: shadow actually steers capital
Cons: more complex; can't execute if shadow's pick has no viable
entry pair this cycle

### Option C — Hybrid (recommended)

First try Option B (target redirect). If redirect isn't possible
(no viable pair, insufficient budget, regime gated), fall back to
Option A (hard veto) — hold cash and wait.

## Chosen design

**Option C.**

## Acceptance criteria

1. A new helper `shadow_preferred_entry(unified, eligible, analyses, cash_usd, max_position_value)`
   returns either:
   - an order dict to place (redirect target was viable), OR
   - the sentinel `{"action": "veto"}` (no viable redirect, block entry), OR
   - `None` (shadow has no strong preference, let live proceed).
2. The Step 5b entry code path calls this helper. If it returns an
   order, use that instead of live's pick. If it returns veto, skip
   entry. If it returns None, use live's pick as before.
3. The existing narrow USD-veto is replaced by this broader helper —
   the USD case is just one outcome (veto, because there's no USD/USD
   pair to redirect to).
4. Every redirect/veto/allow decision is logged with:
   - shadow's best asset and top3_mean
   - live's original pick (if any)
   - the final outcome
5. Threshold for "strong enough to veto": top3_mean ≥ 0.65. Below
   that, shadow is not confident, and live wins by default.
6. Dry-run cycles demonstrate:
   - Veto-fires case (shadow says USD, or no viable redirect)
   - Redirect-fires case (shadow says BTC and BTC/USD clears regime gate)
   - Allow case (shadow top3m < 0.65, live pick passes through)
7. No regression in rotations or exits — this spec only touches
   entry (Step 5b), not 5a/5c.

## Non-goals

- Do not modify the rotation logic in Step 5a.
- Do not modify the exit logic in Step 5c.
- Do not change the shadow scoring aggregator (`compute_unified_holds`).
- Do not touch shadow memory persistence.
- Do not modify the USD-specific volatility handling.

## Evidence

- `state/cc-reviews/brain_2026-04-12_*.md` — 11 consecutive cycles
  where shadow picked BTC/ETH but live deployed into alts.
- `scripts/analyze_shadow.py --hours 24` — 0/11 agreement on order
  cycles, shadow picks BTC (55%) or ETH (45%).
- Backfill with 6h forward window (see spec 05) — empirical evidence
  on whether redirecting to shadow's pick would have outperformed
  live's picks historically.
