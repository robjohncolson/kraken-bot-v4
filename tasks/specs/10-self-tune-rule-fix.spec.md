# Spec 10 — Fix the backwards self-tune rule

## Problem

`self_tune()` in `scripts/cc_brain.py` has a rule that fires when fees
consume too much of gross wins:

```python
if gross_wins > 0 and total_fees / gross_wins > 0.60 and MAX_POSITION_PCT < _POSITION_PCT_MAX:
    old = MAX_POSITION_PCT
    MAX_POSITION_PCT = round(MAX_POSITION_PCT + 0.01, 2)
```

This fires when `fees/gross_wins > 60%` and bumps `MAX_POSITION_PCT`
upward. The reasoning is that bigger positions have better fee-to-P&L
ratios at fixed fee rates.

**But this is mathematically wrong.** If every trade pays the same
**percentage** fee (which is exactly how Kraken's fees work), doubling
position size doubles both the fees AND the gross P&L. The ratio
`fees / gross_wins` stays the same.

What actually changes when positions get bigger:
- **Absolute P&L scales** — bigger wins, bigger losses
- **Drawdown risk scales** — one bad trade eats more of the portfolio
- **Fee ratio stays the same** — the rule never converges

Evidence: over recent cycles, the rule has bumped `MAX_POSITION_PCT`
from 0.04 → 0.05 → 0.06 → 0.07 without changing the underlying
fees/gross_wins ratio (still stuck near 84%). It's a feedback loop
that increases per-trade risk without fixing anything.

## Desired outcome

The self-tune rule is either:
- **Removed** (simplest — leaves `MAX_POSITION_PCT` tuned manually by
  spec 08 / human decision)
- **Fixed to adjust a lever that actually changes the fee ratio**
  (e.g., `ENTRY_PRICE_BUFFER_BPS` from spec 08, or `ENTRY_THRESHOLD`
  to require stronger signals that justify fee burden)

## Acceptance criteria

1. The `if gross_wins > 0 and total_fees / gross_wins > 0.60` branch
   in `self_tune` is either deleted or replaced with a correct rule.
2. If deleted: a comment explains why ("Fee ratio is invariant to
   position size; tune ENTRY_PRICE_BUFFER_BPS or ENTRY_THRESHOLD
   manually via spec 08 / 10").
3. If replaced: the new rule must adjust a lever that can actually
   reduce the fee ratio. Candidates:
   - Bump `ENTRY_THRESHOLD` upward (require stronger signals so only
     high-conviction trades incur fees)
   - Reduce `ENTRY_PRICE_BUFFER_BPS` if it's above some floor
4. A sanity test: a synthetic outcomes set with 50% fee ratio and
   position size X, then the same set with position size 2X — both
   must produce the same tuning decision (or no decision if the rule
   is a no-op for these inputs).
5. The existing `MAX_POSITION_PCT` value is NOT reset. Whatever the
   self-tune has pushed it to stays in place — this spec just stops
   the rule from pushing it further.

## Non-goals

- Do not touch other self-tune rules (win rate, stop-loss rate).
- Do not change `ENTRY_THRESHOLD` manually as part of this spec.
- Do not revert `MAX_POSITION_PCT` to its original 0.04 value.
- Do not remove the entire `self_tune` function.

## Evidence

- `scripts/cc_brain.py` line ~711 — the rule
- `state/cc-reviews/brain_*.md` — multiple reports showing
  `TUNE: MAX_POSITION_PCT X.XX -> Y.YY (fees=84% of wins)` each bumping
  up without converging
