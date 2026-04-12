# Spec 25 -- Quote-currency substitution for high-conviction USDT-quoted entries

## Problem

The brain's pair scanner discovered `ALEO/USDT` with score 1.00 (max possible) at 22:59 UTC and tried to enter. Failed with:

```
FAILED: ALEO/USDT -- Exchange error: EOrder:Insufficient funds
```

Why: the bot has $35.22 USD but only $5.17 USDT. The brain proposed a $18.77 USDT entry on a USDT-quoted pair without checking quote-currency inventory.

This will recur every cycle as long as ALEO has high signal AND the bot doesn't auto-rebalance USD → USDT. Spec 12's permission blacklist doesn't catch it because it's not a permission error -- it's an inventory error. Spec 07's ordermin precheck doesn't catch it either (the qty is fine, the FUNDS aren't).

Already hit twice (20:49 UTC and 22:59 UTC). One more cycle and the orchestrator's tactical priority rule 2 ("same Kraken error >= 3 cycles in a row") fires.

## Desired outcome

When the brain wants to place a high-conviction entry on a `<base>/<quote>` pair where the bot's quote-currency inventory is insufficient, it should:

1. **First**: check if a USD-quoted alternative exists for the same base asset (e.g. `ALEO/USD` instead of `ALEO/USDT`). If yes, switch to that.
2. **If no USD alternative**: skip the entry with a clear log message AND write a `cc_memory` entry with category=`insufficient_quote_inventory` so the orchestrator can see the pattern (and so dedupe can prevent re-proposal next cycle).

This is NOT auto-rebalancing USD → USDT (that's too aggressive a strategy change for an unattended fix). It's quote-currency-aware proposal logic.

## Acceptance criteria

1. In `scripts/cc_brain.py`, find the entry placement logic (around the line that builds the `orders_to_place` list with the entry decision). Before adding an entry to `orders_to_place`:
   - If the pair's quote currency is NOT `USD`, check the bot's available inventory in that quote currency via `/api/balances` (which returns asset balances).
   - If quote currency inventory < required entry size, attempt substitution:
     - Look for an alternative pair `<base>/USD` in the discovered pairs list
     - If found, swap the entry to that pair
     - If not found, skip the entry and write a `cc_memory` entry: `category='insufficient_quote_inventory', pair=<original_pair>, content={base, quote, available, required}`
2. The substitution is applied at the SINGLE chokepoint (just before `orders_to_place.append`), so all proposal paths benefit.
3. Add a regression test in `tests/test_cc_brain_quote_substitution.py`:
   - Mock balances showing $5 USDT, $35 USD
   - Mock pair scanner returning `ALEO/USDT` (score 1.00) and `ALEO/USD` (score 0.95) as available pairs
   - Run the entry decision logic
   - Assert that the placed order is `ALEO/USD`, not `ALEO/USDT`
   - Mock a second case: only `ALEO/USDT` available (no USD alternative)
   - Assert no order placed AND a `cc_memory` `insufficient_quote_inventory` row was written
4. Full pytest suite green.
5. After the fix, the brain's `Pending orders blocking re-proposal` machinery (spec 12) should also pick up `insufficient_quote_inventory` to prevent repeated proposals: extend the existing blocked-pair set to include pairs with this memory category.

## Non-goals

- Do not implement USD -> USDT auto-conversion. That's a strategy decision.
- Do not change the score / signal calculation. The score for ALEO/USDT was correct (1.00).
- Do not modify Kraken inventory queries beyond reading existing `/api/balances`.
- Do not retroactively cancel the existing failed-entry attempts.
- Do not address the case where USDT inventory IS sufficient but the substituted USD pair has worse signal (that's a strategy refinement).

## Files in scope

- `scripts/cc_brain.py`
- `tests/test_cc_brain_quote_substitution.py` (new)
- `tasks/specs/25-aleo-usdt-quote-currency-substitution.result.md`

## Evidence

- `state/cc-reviews/brain_2026-04-12_2049.md`: first failure
- `state/cc-reviews/brain_2026-04-12_2259.md`: second failure
- Quote currency inventory: `/api/balances` shows USDT balance ~$5.17
- Pair scanner discovered ALEO/USDT in pass 2 with score 1.00; no `ALEO/USD` in the same scan (would need to verify)
