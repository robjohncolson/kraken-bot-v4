# Spec 11 — Fix runtime_loop root-exit unit mixing

## Problem

The `trade_outcomes` table contains rows where `exit_proceeds` holds a
**USDT base quantity** instead of **USD proceeds**, producing impossible
P&L numbers like `−$15.85` on parity-priced stablecoin fills.

Evidence (from spec 09 diagnosis, commit `7ee738a`):

- `trade_outcomes.id=1`, `node_id=root-usd`, pair=`USDT/USD`:
  - `entry_cost = 36.9612` (USD)
  - `exit_proceeds = 21.11138898` (actually the **USDT base qty**, not USD)
  - `net_pnl = −15.84981102` (subtracting unlike units)
- Real fill in `ledger`: `21.11138898 USDT × $0.99965 = $21.10` — a
  legitimate near-parity exit
- The corresponding `root-usdt` row (id=3) is correct and shows the
  expected `−$0.0036` stablecoin drift

The bug lives in `runtime_loop.py`. Two suspect paths:
- `_settle_rotation_fills()` writes `trade_outcomes` for rotation exits
- `_handle_root_expiry()` rewrites root-node accounting fields before exit
- Startup restore rebuilds root nodes from current balances, then
  reattaches persisted fields like `entry_cost` by reused root IDs such
  as `root-usd` / `root-usdt`

Most likely mechanism: a synthetic `root-usd` exit recorded as a
`USDT/USD` trade outcome, with `entry_cost` drawn from USD root
accounting and `exit_proceeds` drawn from the USDT/USD fill quantity.

## Desired outcome

All `trade_outcomes` rows store `exit_proceeds` in the **quote currency
(USD)**, consistent with `entry_cost`. No row ever subtracts USDT qty
from USD cost.

## Acceptance criteria

1. A regression test reproduces the bug: given the raw `ledger`
   entries for the 2026-04-06 cycle, writing a `trade_outcomes` row
   must produce `net_pnl` close to `−$0.0036`, not `−$15.85`.
2. `runtime_loop._settle_rotation_fills()` (or equivalent) computes
   `exit_proceeds` as `qty × exit_price` or equivalent USD-denominated
   value, never a raw base quantity.
3. `runtime_loop._handle_root_expiry()` (or equivalent) emits
   `trade_outcomes` rows with consistent units between `entry_cost`
   and `exit_proceeds`.
4. An existing unit test or new guard prevents regression: a
   property-based check that `abs(net_pnl) / max(entry_cost, 0.01) <
   0.20` for any pair in `{USDT/USD, USDC/USD, DAI/USD, PYUSD/USD}`
   unless the pair actually moved (check via OHLC sanity).
5. The −$15.85 row in the existing `trade_outcomes` table is
   **flagged or corrected** but NOT silently rewritten without audit
   log — the historical row stays but an `anomaly_flag` column (or a
   new `trade_outcomes_anomalies` table) marks it.
6. The self-tune logic that previously fired on this phantom loss
   stops seeing it in its 7-day window (because either the row is
   corrected, filtered, or the new anomaly flag is respected).

## Non-goals

- Do not rewrite the rotation tree architecture.
- Do not change the user-visible P&L display format.
- Do not touch `scripts/cc_brain.py` (self-tune already has a parallel
  fix in spec 10).
- Do not attempt retroactive refund/rebalance of positions — the fills
  themselves are fine, only the accounting row is wrong.

## Evidence

- `tasks/specs/09-usdt-loss-investigation.result.md` — full diagnosis
  from Codex 09
- SQLite: `trade_outcomes` rows 1 and 3 on pair `USDT/USD`
- SQLite: `ledger` entries at `2026-04-06T01:21:34` and `01:25:07`
- SQLite: `orders` `O2AJZ6-DCYGF-7RDHRS` and `OBKVFD-OSVMI-IKR6BR`
