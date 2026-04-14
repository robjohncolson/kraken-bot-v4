# Spec 32 Result

## Root Cause

- The `total_value_usd == 0` restart regression was not caused by `trading/portfolio._total_value_usd(...)`. The real break was constructor-side: `runtime_loop.py:205-208` rebuilds startup state with `Portfolio(cash_usd=usd, cash_doge=doge, positions=persisted_positions)` and leaves `total_value_usd` at the dataclass default unless some later rebalance path runs.
- `/api/balances` also had no API field for the full wallet valuation. The runtime already publishes the rotation-tree valuation into `DashboardState.rotation_tree.total_portfolio_value_usd` via `runtime_loop.py:3463` and `runtime_loop.py:3637`; the balances endpoint now reuses that runtime value so `/api/balances` and `/api/rotation-tree` stay aligned by construction.

## Implemented Changes

- `web/routes.py:259-276` adds helpers to parse the runtime rotation-tree total and treat an empty tree with non-zero cash as "wallet total unavailable".
- `web/routes.py:315` and `web/routes.py:679-695` add a one-time `web_routes` warning fallback and extend `/api/balances` to return:
  - `cash_usd`
  - `total_value_usd`
  - `total_wallet_value_usd`
- `core/types.py:130-159` fixes the portfolio invariant at construction time by deriving `Portfolio.total_value_usd` from cash plus persisted positions whenever callers build a portfolio with the default zero total.
- Added the requested regression tests:
  - `tests/test_web_routes.py:64`
  - `tests/test_web_routes.py:80`
  - `tests/test_web_routes.py:94`
  - `tests/test_trading_portfolio.py:8`

## Acceptance Criteria Mapping

1. `total_wallet_value_usd` is now exposed by `/api/balances` and sourced from the runtime rotation-tree total already used by `/api/rotation-tree`.
2. The underlying zero-total invariant break is fixed at portfolio construction time, which is the path used on restart.
3. `total_value_usd` remains present for backcompat and now defaults to at least the cash value on direct `Portfolio(...)` construction.
4. Migration inventory is listed below.
5. The four requested regression tests were added.
6. I did not run `pytest`; subagent instructions explicitly prohibited verification commands.

## Migration Inventory

- `scripts/dev_loop_health_snapshot.py:200`
  - Direct `/api/balances` parser for `total_value_usd`.
  - Intended view: full-wallet value if this endpoint-derived total is used again.
  - Current behavior: the fallback path at `scripts/dev_loop_health_snapshot.py:291-293` only keeps `cash_usd` and discards the total, so this is a follow-up hardening target rather than an active bug after this patch.
- `scripts/dev_loop.ps1:210`, `scripts/dev_loop.ps1:676`, `scripts/dev_loop.ps1:686`
  - Indirect consumer through `dev_loop_health_snapshot.py`.
  - Intended view: full-wallet value.
  - Migration depends on the wrapper above, not on a direct `/api/balances` change in this spec.
- `scripts/cc_brain.py:1448-1450`
  - Direct `/api/balances` consumer, but it only reads `cash_usd`.
  - Intended view: cash-only on that fallback path.
  - No `total_wallet_value_usd` migration needed in this spec.
- `tui/state.py:198`, `tui/widgets/portfolio.py:52`, `tui/screens/dashboard.py:250`, `tui/screens/holdings.py:147`
  - These are not `/api/balances` consumers; they read `total_value_usd` from `/api/portfolio` or SSE state.
  - Intended view:
    - `tui/widgets/portfolio.py` already prefers `rotation_tree.total_portfolio_value_usd` when present, which is the full-wallet view.
    - The dashboard/holdings fallbacks still use positions-only `portfolio.total_value_usd`.
  - Follow-up needed if the TUI should display `total_wallet_value_usd` outside the rotation-tree-aware widget path.
- `web/static/app.js:131`
  - Not a `/api/balances` consumer; this is the dashboard `/api/portfolio` view.
  - Intended view depends on product choice:
    - keep positions-only if the card is meant to mirror `Portfolio`
    - migrate in a follow-up if the dashboard should show full-wallet value instead
- `scripts/dev_loop_prompt.md:41` and `scripts/dev_loop_weekly_prompt.md:44`
  - Prompt/docs references only, not runtime consumers.

## Notes

- GitNexus MCP impact calls were unavailable in this subagent session (`user cancelled MCP tool call`), so I used local code search to trace the affected call sites before editing.
