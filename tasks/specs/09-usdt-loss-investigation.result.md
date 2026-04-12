# USDT/USD -$15.85 investigation

## Summary
- Classification: **A) accounting bug**
- Code fix applied here: **No**
- Reason no fix was applied: the defect points to `runtime_loop.py`, which is outside the owned paths for this task. Per the task instructions, I stopped at diagnosis and documentation.

## Evidence collected
- Memory API (`/api/memory`) repeatedly emitted two USDT/USD postmortems:
  - `pnl=-0.0036`
  - `pnl=-15.8498`
- SQLite `trade_outcomes` contains two `USDT/USD` rows from the same close cycle:
  - `id=1`, `node_id=root-usd`, `entry_cost=36.9612`, `exit_proceeds=21.11138898`, `net_pnl=-15.84981102`
  - `id=3`, `node_id=root-usdt`, `entry_cost=17.0046962791822`, `exit_proceeds=17.0011247929555000`, `net_pnl=-0.0035714862267000`
- SQLite `orders` for that cycle:
  - `O2AJZ6-DCYGF-7RDHRS` `root-usd` `USDT/USD` `rotation_exit` `buy` `base_qty=21.11138898614515080278097334` `limit_price=0.99965`
  - `OBKVFD-OSVMI-IKR6BR` `root-usdt` `USDT/USD` `rotation_exit` `sell` `base_qty=17.00707727` `limit_price=0.99965`
- SQLite `ledger` for that cycle:
  - `USDT/USD buy 21.11138898 @ 0.99965000 fee 0.04220800 filled_at 2026-04-06T01:25:07.749525Z`
  - `USDT/USD sell 17.00707727 @ 0.99965000 fee 0.03400225 filled_at 2026-04-06T01:21:34.580498Z`
- The original `brain_2026-04-06_*` cycle reports are not present under `state/cc-reviews`, so the raw reconstruction had to come from SQLite plus later brain reports that surfaced the postmortem.

## Entry details
- There is **no evidence of a real 42.9% losing USDT/USD execution**.
- The only real `USDT/USD` fills in SQLite are near-parity stablecoin fills at `0.99965`.
- The downstream brain report that exposed the problem is `brain_2026-04-11_1935.md`, which lists both:
  - `USDT/USD lost $0.0036`
  - `USDT/USD lost $15.8498`

## Exit details
- Real fill 1:
  - Cycle timestamp from ledger: `2026-04-06T01:25:07.749525Z`
  - Txid: `O2AJZ6-DCYGF-7RDHRS`
  - Pair: `USDT/USD`
  - Side: `buy`
  - Qty: `21.11138898` USDT
  - Price: `$0.99965`
  - Gross USD spent: `21.11138898 * 0.99965 = $21.103999993857`
  - Fee: `$0.04220800`
- Real fill 2:
  - Cycle timestamp from ledger: `2026-04-06T01:21:34.580498Z`
  - Txid: `OBKVFD-OSVMI-IKR6BR`
  - Pair: `USDT/USD`
  - Side: `sell`
  - Qty: `17.00707727` USDT
  - Price: `$0.99965`
  - Gross USD proceeds: `17.00707727 * 0.99965 = $17.0011247929555`
  - Fee: `$0.03400225`
  - Exit reason in `trade_outcomes`: `root_exit_bearish`

## Math
- Reported bad row (`trade_outcomes.id=1`, `node_id=root-usd`):
  - Entry cost: `$36.9612`
  - Exit proceeds: `21.11138898`
  - Stored net P&L: `21.11138898 - 36.9612 = -15.84981102`
- Why that math is invalid:
  - `entry_cost` is in **USD**
  - `exit_proceeds` for this row is the **USDT base quantity**
  - The row is subtracting unlike units
- What the raw fill says instead:
  - The actual `USDT/USD` buy fill was near parity: `$21.1040` gross spent for `21.11138898` USDT
  - A stablecoin fill at `0.99965` cannot produce a real `$15.85` loss on a `$36.96` cost basis
- The second `USDT/USD` row (`id=3`, `node_id=root-usdt`) is the only row whose magnitude looks like a normal stablecoin spread/fee loss.

## Root cause
**A) Accounting bug**

The outlier comes from the **root rotation outcome accounting path**, not from market execution.

Two upstream facts point to that:

1. `web/routes.py` is not computing the loss.
   - `/api/trade-outcomes` simply returns rows from SQLite.
   - The bogus number already exists in `trade_outcomes`.

2. The write path is in `runtime_loop.py`, outside the owned paths.
   - `runtime_loop.py:_settle_rotation_fills()` writes `trade_outcomes` rows for rotation exits.
   - `runtime_loop.py:_handle_root_expiry()` rewrites root-node accounting fields before exit.
   - Startup restore logic rebuilds root nodes from current balances, then reattaches persisted fields like `entry_cost` by reused root IDs such as `root-usd` / `root-usdt`.

Most likely failure mode for this specific row:
- a synthetic root-level `root-usd` exit was recorded as a normal `USDT/USD` trade outcome
- its `entry_cost` came from root accounting state in USD
- its `exit_proceeds` came from the exit fill quantity on `USDT/USD`
- those values were stored in incompatible terms, producing the impossible `-$15.85`

This is consistent with the stored row, the raw ledger fills, and the fact that the same cycle also emitted a normal small-loss `root-usdt` row.

## Hypothesis mapping
- Hypothesis 1 partial-fill accounting error: **not supported**
- Hypothesis 2 quantity mismatch at exit: **not supported as the primary cause**
- Hypothesis 3 root-exit lumping: **partially supported**
- Hypothesis 4 stablecoin mis-pricing: **not supported**
- Hypothesis 5 currency conversion bug: **closest match**

Best fit: **root-exit accounting mixed synthetic root accounting with real `USDT/USD` fill data**.

## Affected code path
- Producer of the bad row: `runtime_loop.py`
- Reader/API surface: `web/routes.py` only exposes the already-bad row
- Persistence layer: `persistence/sqlite.py` inserts exactly what runtime supplies; it does not derive the bad math itself

## Fix
- **No code change made in this task**
- Reason: the root cause points to `runtime_loop.py`, which is outside the allowed owned paths

## Recommended follow-up
1. Fix root exit accounting in `runtime_loop.py` so quote-side root exits do not write mixed-unit `trade_outcomes` rows.
2. Stop restoring root accounting fields solely by reused root node IDs (`root-usd`, `root-usdt`, etc.) without verifying the root instance still represents the same position state.
3. Add a regression test around root exits on stablecoin pairs:
   - if `pair in {"USDT/USD","USDC/USD","DAI/USD"}` and `abs(net_pnl / entry_cost) > 0.10`, flag the outcome as anomalous and exclude it from self-tune/postmortem summaries until the producer is fixed.
