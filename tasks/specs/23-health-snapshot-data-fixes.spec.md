# Spec 23 -- Fix wrong source-data lookups in the health snapshot

## Problem

Spec 22 added the health snapshot script. It works structurally but uses wrong source-data lookups for two fields:

1. `open_positions` reads from the `positions` table -- but the bot uses `rotation_nodes`. The script returns 0; reality is 7 open root nodes (out of 20 total).
2. `current_total_value_usd` reads from `/api/balances` which only reports the USD cash balance (~$35), not the full portfolio value (~$472). The full value is in the latest `portfolio_snapshot` memory or computable from `/api/rotation-tree`.

The other fields are correct:
- `permission_blocked_pairs` = 0 because no AUD/USD attempts have happened since the spec-12 restart (the brain hasn't tried because the rotation evaluator hasn't proposed AUD exit recently). This will populate when the next attempt fails.
- `recon_errors_24h` = 0 because the bot doesn't write `reconciliation_anomaly` memories yet -- the warnings are only logged, not persisted.

## Desired outcome

The health snapshot script returns correct values for `open_positions` and `current_total_value_usd`. The script also reports `holdings_count` (matching the brain's portfolio_snapshot memory shape) so the orchestrator can see "20 holdings, 7 actively rotating".

## Acceptance criteria

1. `scripts/dev_loop_health_snapshot.py` is updated:
   - **`open_positions`**: query `rotation_nodes` for `WHERE status = 'open' AND depth = 0` (root nodes that are actively rotating). Return that count. Also report `total_root_positions = COUNT(WHERE depth = 0)`.
   - **`current_total_value_usd`**: read from the most recent `cc_memory` row with `category = 'portfolio_snapshot'`. Parse the `content` JSON and pull `portfolio_value_usd`. Fallback to `null` if no snapshot exists.
   - **`current_cash_usd`**: same source (latest portfolio_snapshot memory's `cash_usd` field). Falls back to `/api/balances` if no memory exists.
   - **NEW field `holdings_count`**: pull from the same portfolio_snapshot memory. This matches the brain's notion of "how many distinct asset positions does the bot have".
2. The output JSON has the same structure as before plus the new `holdings_count` and `total_root_positions` fields.
3. The wrapper in `scripts/dev_loop.ps1` updates the HEALTH SNAPSHOT formatting block to include the new fields:
   ```
   - Open positions:    7 of 20 root (holdings: 20)
   - Cash:    $35.03   |   Total value:    $472.00
   ```
4. Both prompts (`dev_loop_prompt.md` and `dev_loop_weekly_prompt.md`) need NO changes -- they reference the snapshot generically.
5. After the patch, `python scripts/dev_loop_health_snapshot.py data/bot.db http://127.0.0.1:58392/api/balances` returns realistic values:
   - `open_positions: 7`
   - `total_root_positions: 20`
   - `holdings_count: 20`
   - `current_total_value_usd: ~$471` (not ~$35)
6. The python script still parses cleanly via `python -c "import ast; ast.parse(open(...).read())"`.

## Non-goals

- Do not change the bot's `/api/balances` to report total portfolio value. That's a separate concern.
- Do not implement reconciliation_anomaly persistence in the bot. Wait until it's actually needed.
- Do not change the snapshot's other fields (P&L, win rates, etc. -- those work correctly).
- Do not modify the wrapper's overall flow or other helpers.

## Files in scope

- `scripts/dev_loop_health_snapshot.py` (the python script)
- `scripts/dev_loop.ps1` (just the snapshot section formatting block)
- `tasks/specs/23-health-snapshot-data-fixes.result.md`

## Evidence

- `data/bot.db` `rotation_nodes` schema confirmed: has `status` column, has `depth` column. `WHERE status='open' AND depth=0` returns 7 rows.
- Latest `cc_memory` with `category='portfolio_snapshot'`: `{"portfolio_value_usd": 471.71, "cash_usd": 35.22, "holdings_count": 20, "total_trades_7d": 14}` from 2026-04-12T20:53Z.
- Spec 22 result file documented these as known data quirks worth a follow-up.
