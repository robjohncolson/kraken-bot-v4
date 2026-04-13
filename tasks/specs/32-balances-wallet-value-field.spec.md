# Spec 32 -- /api/balances total_wallet_value_usd correctness

## Problem

`/api/balances` misreports total portfolio value:

- Post-spec-29 restart: `{"cash_usd":"256.3707","total_value_usd":"0"}` -- `total_value_usd` is 0 despite `cash_usd` being ~$256. Both fields read from the SAME `portfolio` object in `web/routes.py:656-660`:
  ```python
  portfolio = state.portfolio
  return {
      "cash_usd": str(portfolio.cash_usd) if portfolio else "0",
      "total_value_usd": str(portfolio.total_value_usd) if portfolio else "0",
  }
  ```
  So `portfolio` is non-None (cash_usd is populated) but `portfolio.total_value_usd` returns 0. That's a legit bug in how `total_value_usd` is computed on the `Portfolio` model -- it should be at least `>= cash_usd`.

- Pre-restart the same endpoint returned `total_value_usd=$256.76` -- essentially just `cash_usd + tiny dust`, still missing the ~$176 of non-position wallet holdings (ADA, HYPE, MON, SOL, XRP, GBP). This is because the `Portfolio` model only counts `cash_usd + sum(active positions)`, and those assets aren't tracked as positions -- they're raw wallet balances held outside the rotation planner.

Meanwhile `/api/rotation-tree` correctly shows `total_portfolio_value_usd=$432.05` by walking rotation roots and summing `quantity * current_price`. That is the true wallet value. Downstream consumers that trust `/api/balances.total_value_usd` (including `scripts/dev_loop_health_snapshot.py`, `scripts/cc_brain.py`, and the TUI) see the wrong number.

## Desired outcome

1. `/api/balances` returns a correct total that equals the actual wallet value (cash + all held asset values at current prices), agreeing with `/api/rotation-tree.total_portfolio_value_usd` to within $1.
2. The underlying `portfolio.total_value_usd` bug (returning 0 despite non-zero cash) is either fixed or bypassed -- documented in the result file.
3. Downstream consumers of `/api/balances.total_value_usd` see the correct number without code changes on their side (no API rename unless backcompat is preserved).

## Acceptance criteria

1. **Add `total_wallet_value_usd` field** to the `/api/balances` response in `web/routes.py:get_balances()`:
   - The value is the sum of `cash_usd + sum(wallet_balance_asset * current_price_usd)` across all reconciled Kraken balances, where `wallet_balance_asset` comes from the live reconciled balance state (not the `Portfolio` positions dict).
   - Source the balance state the same way `/api/rotation-tree` does in `runtime_loop.py` -- use the same data the tree valuation uses, so the two endpoints agree by construction.
   - If the reconciled balance state is unavailable (startup transient), fall back to `cash_usd` and log a single `web_routes` warning `"balances endpoint: wallet total unavailable, returning cash only"`.

2. **Diagnose and fix `portfolio.total_value_usd` returning 0 despite cash != 0**. Likely path: `trading/portfolio.py:180` or thereabouts computes `total_value_usd = _total_value_usd(cash_usd, positions)`. If positions is empty and the function returns 0 instead of cash_usd, that's the bug. Fix it so the invariant `total_value_usd >= cash_usd` is never violated.
   - If the root cause is in the `Portfolio` construction (e.g. reconciler passing None/empty instead of actual cash), document it in the result file and fix at the source.
   - If the root cause is in the `_total_value_usd` helper, fix it there.
   - In either case, add or extend a test that asserts `Portfolio(cash=100, positions={}).total_value_usd >= 100`.

3. **Update `/api/balances` response shape** to include BOTH fields:
   ```json
   {
     "cash_usd": "256.3707",
     "total_value_usd": "<fixed value -- cash + positions>",
     "total_wallet_value_usd": "<cash + all wallet holdings at current prices, matches tree>"
   }
   ```
   Keep `total_value_usd` as the cash-plus-positions view (now fixed to be non-zero). Add `total_wallet_value_usd` as the correct full-wallet view.

4. **Document which consumers should migrate to the new field** in the result file: list every grep hit for `total_value_usd` in `scripts/`, `tui/`, and any other caller. For each, note whether they want the positions-only view or the full-wallet view. Do not modify them in this spec -- just document. Migration is a follow-up.

5. **Tests**:
   - `tests/test_web_routes.py::test_balances_includes_total_wallet_value_usd`: mock a state with cash=$100, positions={}, and raw balances {ADA: 50} at price $0.25; assert response includes `total_wallet_value_usd="112.50"`.
   - `tests/test_web_routes.py::test_balances_total_value_usd_at_least_cash`: mock cash=$256.37, empty positions; assert `total_value_usd >= "256.37"` (no more 0 regression).
   - `tests/test_trading_portfolio.py` (or the existing portfolio test file if it has one): add a test asserting `Portfolio(...).total_value_usd >= cash_usd` invariant.
   - `tests/test_web_routes.py::test_balances_wallet_matches_rotation_tree`: mock runtime with a known tree state; assert `/api/balances.total_wallet_value_usd` matches `/api/rotation-tree.total_portfolio_value_usd`.

6. Full pytest green.

## Non-goals

- Do not rename `total_value_usd`. Legacy consumers depend on the name.
- Do not migrate any callers in this spec. Document the list of callers; migration is a follow-up spec.
- Do not modify `/api/rotation-tree` at all. It is already the source of truth.
- Do not touch `Portfolio.positions` shape or meaning. Positions stay as "things with entry provenance".
- Do not add a new field to `Portfolio` itself. The new total lives at the API layer, not in the model.
- Do not restart the bot in this spec. Parent agent handles that.

## Files in scope

- `web/routes.py` (the balances endpoint)
- `trading/portfolio.py` (the underlying total_value_usd fix)
- `tests/test_web_routes.py` (new tests, create file if it doesn't exist)
- `tests/test_trading_portfolio.py` (if exists) or equivalent
- `tasks/specs/32-balances-wallet-value-field.result.md`

Codex may also need to read `runtime_loop.py` to understand how rotation tree valuation sources its balances, but should not modify it.

## Evidence

- Live query post-restart: `curl http://127.0.0.1:58392/api/balances` returned `{"cash_usd":"256.3707","total_value_usd":"0"}`.
- Live query post-restart: `curl http://127.0.0.1:58392/api/rotation-tree` returned `total_portfolio_value_usd="432.05"` with 7 real roots.
- `web/routes.py:656-660` is the handler. Both fields read from `state.portfolio`, so the bug is NOT a portfolio-is-None fallback -- it's a bad `total_value_usd` getter on a non-None portfolio.
- Spec 29 result file documents the finding: "the rotation tree was right all along; balances is under-reporting".
