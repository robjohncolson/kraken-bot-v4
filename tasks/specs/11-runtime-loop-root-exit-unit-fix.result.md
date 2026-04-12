# Spec 11 result

- `runtime_loop.py`
  - Root rotation exits now persist executed quote notional (`fill_qty * fill_price`) instead of calling `exit_proceeds()` on synthetic depth-0 roots.
  - Quote-side roots such as `root-usd -> USDT/USD` now settle `entry_cost` from the executed quote notional, which keeps `entry_cost`, `exit_proceeds`, and `net_pnl` in the same units on the outcome row.
  - `_handle_root_expiry()` now recalculates root `entry_cost` in quote units: base-side roots still use `quantity_total * price`, while quote-side roots keep the held quote amount as-is.

- `persistence/sqlite.py`
  - Added `trade_outcomes.anomaly_flag`.
  - Added stablecoin-parity anomaly detection for depth-0 rows on `USDT/USD`, `USDC/USD`, `DAI/USD`, and `PYUSD/USD`.
  - Schema bootstrap backfills the flag onto existing suspicious rows instead of rewriting historical P&L values.

- Regression coverage
  - Added `tests/test_runtime_loop_root_exit.py` with a USD-root regression that reproduces the bad `USDT/USD` shape (`entry_cost=36.9612`, fill `21.11138898 @ 0.99965`) and asserts the stored row settles near zero instead of `-$15.85`.
  - Added a parity guard across `USDT/USD`, `USDC/USD`, `DAI/USD`, and `PYUSD/USD` asserting `abs(net_pnl) < 10%` of `entry_cost`.
  - Added a legacy-schema migration test that confirms the historical-style outlier is flagged via `anomaly_flag`.

- Not run here
  - `python -m pytest tests/ -x`
  - This subagent task explicitly prohibited running verification commands after patching.
