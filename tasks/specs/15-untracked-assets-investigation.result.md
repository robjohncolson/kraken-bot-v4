# Untracked assets reconciliation warning investigation

## Summary
- Classification: **D) real improvement opportunity**
- Code fix applied here: **No**
- Reason no fix was applied: this task was investigation-only and owned only the result document.
- The recurring warning is **not** benign held-fiat noise. The confirmed untracked assets in the current restart snapshots are `FLOW`, `HYPE`, `MON`, and `TRIA`; the later jump from `4` to `5` is best explained by a newly acquired `ALEO` balance after a CC `ALEO/USDT` order. `AUD`, `CAD`, `EUR`, `GBP`, and `USDT` are already part of the reconciler's tracked asset set via historical SQLite `orders` rows.

## Where it's logged
- Startup warning:
  - `main.py:221` `def _run_reconciliation(...)`
  - `main.py:228` logs `Reconciliation found discrepancies:`
  - `main.py:236` logs `Untracked assets: %d`
- Runtime recurring warning:
  - `runtime_loop.py:959` `async def _handle_effects(...)`
  - `runtime_loop.py:976-979` handles `ReconciliationDiscrepancy`
  - `runtime_loop.py:978` logs `Reconciliation discrepancy detected: %s`
- The summary string that feeds the runtime warning is built at:
  - `scheduler.py:420` `def _reconciliation_summary(...)`
  - `scheduler.py:425` formats `untracked_assets={len(report.untracked_assets)}`
- The reducer-side info log comes from:
  - `core/state_machine.py:511` `def _handle_reconciliation(...)`
  - `core/state_machine.py:556` emits `LogEvent(message=f"reconciliation: {event.summary or 'ok'}")`

## Reconciliation logic explanation
- `persistence/sqlite.py:505-514` loads `RecordedState` from:
  - all open positions via `fetch_positions()`
  - all persisted orders via `fetch_orders()`
- `trading/reconciler.py:391-409` decides `untracked_assets` like this:
  - start with `tracked_assets = {"USD"}` at `trading/reconciler.py:395`
  - add both sides of every persisted position pair
  - add both sides of every persisted order pair
  - flag every positive Kraken balance whose `balance.asset` is **not** in that set
- In exact terms, an asset is "untracked" when Kraken reports a non-zero balance for it, but that asset symbol does not appear in any persisted `positions.pair` or `orders.pair` in SQLite.
- This logic is purely symbol-based. It is **not** checking whether the asset is fiat, quote currency, root inventory, or "expected" by strategy.

## Currently flagged assets
- `bot.db` has **no balances table**. The tracked side comes from SQLite `positions` and `orders`; the balance side is visible in the runtime's startup/restart balance snapshot logs taken immediately before the same `bot.db` is loaded.
- SQLite evidence:
  - `orders` contains fiat/stablecoin pairs for the originally suspected assets:
    - `AUD`: `ADA/AUD`, `BTC/AUD`, `DOGE/AUD`, `ETH/AUD`, `LINK/AUD`, `LTC/AUD`, `PEPE/AUD`, `XRP/AUD`
    - `CAD`: `DOGE/CAD`, `PEPE/CAD`
    - `EUR`: `AKT/EUR`, `SUI/EUR`
    - `GBP`: `ALGO/GBP`, `ATOM/GBP`, `DOGE/GBP`, `KSM/GBP`, `PEPE/GBP`, `SOL/GBP`, `SUI/GBP`, `WIF/GBP`, `XLM/GBP`
    - `USDT`: `ADA/USDT`, `ALGO/USDT`, `DOGE/USDT`, `LTC/USDT`, `RAVE/USDT`, `TON/USDT`, `UNITAS/USDT`, `USDT/USD`, `XMR/USDT`
  - SQLite contains **no** `orders` or open `positions` for `FLOW`, `HYPE`, `MON`, `TRIA`, or `ALEO`.
- Confirmed current `untracked_assets=4` set:
  - `state/scheduled-logs/main_restart_20260412_130900.log:22` `FLOW = 0.1008847354`
  - `state/scheduled-logs/main_restart_20260412_130900.log:24` `HYPE = 0.4613630`
  - `state/scheduled-logs/main_restart_20260412_130900.log:27` `MON = 1756.11225`
  - `state/scheduled-logs/main_restart_20260412_130900.log:31` `TRIA = 0.0001`
  - `state/scheduled-logs/main_restart_20260412_130900.log:46` startup reports `Untracked assets: 4`
- Earlier `untracked_assets=2` also matches this logic:
  - `state/bot-stdout.log:21` `FLOW = 0.1008847354`
  - `state/bot-stdout.log:28` `TRIA = 0.0001`
  - `state/bot-stdout.log:47` startup reports `Untracked assets: 2`
- Later `untracked_assets=5`:
  - `state/scheduled-logs/main_restart_20260412_133621.log:513` logs `CC placed order ... on ALEO/USDT`
  - `state/scheduled-logs/main_restart_20260412_133621.log:521-524` shows the next reconcile moved from `26 balances` / `untracked_assets=4` to `27 balances` / `untracked_assets=5`
  - Because SQLite still has no `ALEO` rows anywhere, the fifth untracked asset is **almost certainly `ALEO`**. This last step is an inference from timing plus the missing SQLite rows, not a directly printed later balance line.

## Root cause / explanation
- The initial diagnosis is incorrect.
- Why the held-fiat theory does not fit:
  - `AUD`, `CAD`, `EUR`, `GBP`, and `USDT` are already present in the SQLite-derived tracked set because `orders` contains historical pairs using all of them.
  - On the 2026-04-11 startup snapshot, those fiat/stablecoin balances were already present, but the reconciler reported only `untracked_assets=2`, which matches `FLOW` and `TRIA`, not fiat.
- What is actually happening:
  - `FLOW` and `TRIA` are legacy/unmanaged balances with no persisted `orders`/`positions` representation in `bot.db`.
  - `MON`, `HYPE`, and likely `ALEO` are intentional holdings acquired through the CC API order path, but that path does not persist an order record into SQLite.
- Code-path proof:
  - `web/routes.py:308-324` `place_order()` calls `executor.execute_order()` and logs `CC placed order ...`, but does **not** call `SqliteWriter.insert_order()` or `upsert_order()`.
  - By contrast, the runtime-managed order path at `runtime_loop.py:986-1008` places the order and immediately calls `self._writer.upsert_order(...)`.
- Result:
  - The reconciler is doing exactly what it was written to do.
  - The blind spot is that some real balances are intentional, but SQLite never learns about them, so the reconciler cannot treat them as tracked.

## Recommendation
- **D) Real improvement opportunity**
- Rationale:
  - This is not just cosmetic wording.
  - It is also not a reducer bug: the reducer/reconciler are correct given the persisted state they receive.
  - The real issue is that the bot's "tracked asset" model is incomplete for:
    - legacy residual balances with no strategy record
    - CC/API-placed orders that bypass SQLite persistence
- Recommended follow-up:
  1. Persist `/api/orders` placements into SQLite using the same `upsert_order()` path that `runtime_loop` uses, so assets like `MON`, `HYPE`, and `ALEO` stop appearing as untracked immediately after intentional entries.
  2. Decide how to model tolerated residual balances like `FLOW` and `TRIA`:
     - import them into a known-holdings table / root-holdings model, or
     - explicitly allowlist them in reconciliation
  3. Only after the model is fixed, consider softening the warning text when the report contains only low-severity `untracked_assets` and no ghost positions / foreign orders / fee drift.
