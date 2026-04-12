# Shadow Mode Promotion Criterion

## What shadow mode is

Every cc_brain cycle computes two parallel decision paths:

- **Live** — current `evaluate_portfolio` + `ENTRY_THRESHOLD` logic, which
  treats USD/USDT/USDC as a special "cash deployment" path and only scores
  rotations between held crypto.
- **Shadow** — `compute_unified_holds()` scores every asset (USD included)
  via bidirectional pair analysis, aggregating across all pairs that touch
  the asset. Currency-agnostic by construction.

Shadow verdicts are persisted as `shadow_verdict` memories and can be
reviewed with `python scripts/analyze_shadow.py`.

## Why a promotion bar

We don't flip over to the unified path on vibes. Shadow needs to earn it
by demonstrating — in live market conditions, not backtests — that its
verdicts are at least as good as the live path's decisions, ideally better,
across a large enough sample to be more than noise.

## The bar — all three must hold before promotion

### 1. Coverage: shadow actually has something to say

- **≥ 20 cycles** of shadow verdicts captured (≥ 40 hours of live operation
  at current 2h cadence).
- **≥ 5 distinct "best_shadow_hold" picks** (not just always USD or always
  BTC — the aggregator has to be discriminating).
- **Every held asset has n ≥ 3 coverage in ≥ 70% of cycles** (otherwise the
  shadow verdict is missing the data it needs for real decisions).

### 2. Agreement quality: disagreements are useful, not random

Disagreements are the whole point of shadow mode — we want shadow to catch
things live misses. But disagreements need to be _informative_, not noise.

- **If live and shadow agree ≥ 80% of the time**: the unified path is a
  strict improvement (it sees more signals without systematically
  disagreeing). Promote.
- **If 50-80% agreement**: look at the disagreements. For each, check the
  actual 6-hour forward return of the asset each path picked. If shadow's
  picks beat live's picks on net across the disagreement set, promote.
- **If < 50% agreement**: something is fundamentally different about the
  two paths. Do not promote without digging into root cause.

### 3. Live decisions don't degrade

Shadow mode is additive — it adds cross-pair analysis and logging but does
not touch `orders_to_place`. Before promoting:

- The current live path's win-rate, P&L, and fee-adjusted returns (measured
  over the same shadow-observation window) should be **within ±10% of the
  pre-shadow baseline**. If the extra Kronos calls slow the cycle enough
  that live decisions drift, that's a regression we need to fix before
  promoting.

## Evaluation procedure

After ≥ 20 cycles of shadow memories have accumulated:

```bash
python scripts/analyze_shadow.py --hours 72
```

Then look at each section:

| Section | What to check |
|---------|--------------|
| Live decision types | Mostly `order`, some `hold`. If all `hold`, the bar is trivially met. |
| Shadow 'best hold' picks | Distribution — not single-asset domination. |
| Live decision targets | Compare to shadow picks. |
| Agreement on order cycles | Primary signal. ≥ 80% = promote. |
| Top disagreement patterns | Which asset does shadow prefer over live? |
| Eligibility coverage | Every held asset should be ≥ 70%. |
| Held-asset shadow top3_mean | Should correlate with asset's actual performance. |

## Manual disagreement analysis

For each disagreement in the top patterns list:

1. Look up the cycle timestamp in `shadow_verdict` memory content.
2. Note the asset live picked and the asset shadow picked.
3. Check Kraken OHLCV 6 hours _after_ that cycle.
4. Which asset actually performed better in the 6 hours following?
5. Tally: shadow right, live right, or tie (within ±0.5%).

Criterion: shadow correct on **at least 55%** of non-tie disagreements.

## What promotion looks like

If the bar clears:

1. Replace Step 5a's `evaluate_portfolio` call with a unified rotation loop
   that uses `compute_unified_holds` output.
2. Delete Step 5b's `ENTRY_THRESHOLD` check — USD becomes just another asset
   in the rotation loop.
3. Keep the shadow logging block, but flip it: the _old_ path becomes
   shadow, the unified path becomes live. Run another ~20 cycles in that
   configuration before deleting the legacy code entirely.

## What failure looks like

If the bar does not clear after 40+ cycles:

- **Low agreement (< 50%)**: find out _why_. Is the inversion broken?
  Is `100 - RSI` too rough? Are Kronos/TimesFM direction-flips producing
  nonsense? Add logging to the disagreement cases and investigate.
- **Shadow systematically wrong on disagreements**: the unified aggregator
  has a bias. Consider: different aggregation (median? volume-weighted?
  weight-by-confidence?), stricter min-n, or regime-adjusted weighting.
- **Live degrades from cycle-time overhead**: reduce Pass 2 cross budget
  from 20 → 10, or cache analyses across cycles.

## Current state (as of 2026-04-11)

- Shadow code landed in commit `06fb1b1`.
- 1 shadow verdict captured so far (dry-run test).
- ~19 more cycles needed before first evaluation.
