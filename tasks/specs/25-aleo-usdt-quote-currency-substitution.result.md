# Spec 25 - Quote-currency substitution for high-conviction USDT entries

Implemented the entry chokepoint fix in `scripts/cc_brain.py`.

- Non-USD entries now check available quote inventory via `/api/exchange-balances`.
- If the quote balance is short, the brain tries `<base>/USD` when that pair was analyzed and scored at least `0.50`.
- If no viable USD substitute exists, the brain skips the entry, logs `INSUFFICIENT_QUOTE`, and writes `cc_memory.category='insufficient_quote_inventory'`.
- The blocked-pair loader now includes `insufficient_quote_inventory`, so later cycles skip those pairs without re-proposing them.
- Added regression tests in `tests/test_cc_brain_quote_substitution.py` for substitution, skip-and-memory-write, and blocked-by-memory behavior.

Validation was not run in this subagent because the task wrapper prohibited tests, lint, and other verification commands.
