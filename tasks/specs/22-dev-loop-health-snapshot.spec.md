# Spec 22 -- Inject a precomputed health snapshot into the runtime context

## Problem

The CC orchestrator's Step 1 (Observe) currently delegates everything to the LLM: "tail 12 brain reports, query SQLite, check git log." This is flexible but inefficient and inconsistent:

1. **Cost**: each run uses ~250-300k input tokens just to ingest brain reports + SQLite results. Most of that is the same data the previous run already saw.
2. **Inconsistency**: the LLM picks different things to focus on across runs, leading to stochastic verdicts (today's dry runs alternated between `no_action` and `would_dispatch` on the same data).
3. **No P&L visibility**: the priority list focuses on errors. The orchestrator can't easily compare 7-day P&L to the prior 7 days, or notice "win rate dropped 10pp this week".
4. **No deltas**: the LLM has to compute trends from raw data each run instead of seeing precomputed deltas.

## Desired outcome

The wrapper computes a structured "HEALTH SNAPSHOT" from SQLite directly and injects it into the runtime context block alongside `last_code_commit_ts` and `RECENT DISPATCH HISTORY`. The LLM gets a one-page view of bot health with deltas and can diagnose faster + more consistently.

## Acceptance criteria

1. `scripts/dev_loop.ps1` adds a helper `Get-BotHealthSnapshot` that:
   - Connects to `data/bot.db` via a python -c subprocess (similar to `Test-PreviousSpecSettled`)
   - Computes these metrics:
     - `total_trades_24h`: count of `trade_outcomes` rows where `closed_at > now - 24h`
     - `total_trades_7d`: same with 7d window
     - `total_trades_prior_7d`: same with 7d-14d window (for delta)
     - `net_pnl_24h`: SUM(net_pnl) on closed trades in last 24h, treating `anomaly_flag` rows as $0
     - `net_pnl_7d`: same with 7d window
     - `net_pnl_prior_7d`: same with 7d-14d window (for delta)
     - `win_rate_7d`: count(net_pnl > 0) / count(*) for last 7d closed trades
     - `win_rate_prior_7d`: same with 7d-14d window
     - `recon_errors_24h`: count of `cc_memory` rows with `category='reconciliation_anomaly'` in last 24h
     - `permission_blocked_pairs`: count of distinct pairs in `cc_memory.category='permission_blocked'`
     - `open_positions`: count of `positions` rows where `status` is open or unset (use the actual schema; check `PRAGMA table_info(positions)` for the exact column)
     - `current_cash_usd`: from the bot's `/api/balances` endpoint (curl)
     - `current_total_value_usd`: same
   - Returns a PSCustomObject with all these fields. On any error, log and return `$null` (snapshot is optional, not blocking).
2. The runtime context block built before invoking claude now includes a "HEALTH SNAPSHOT" subsection AFTER the dispatch history:
   ```
   ## HEALTH SNAPSHOT (computed by wrapper from SQLite + /api/balances)
   - Trades 24h:    4    (7d: 14, prior 7d: 11)
   - Net P&L 24h:   -$2.13   (7d: -$14.59, prior 7d: -$3.21)  Delta 7d: -$11.38
   - Win rate 7d:   43%   (prior 7d: 55%)  Delta: -12pp
   - Recon errors 24h:    12
   - Permission-blocked pairs:    1
   - Open positions:    19
   - Cash:    $35.03   |   Total value:    $473.28
   ```
3. `scripts/dev_loop_prompt.md` Step 1 (Observe) gains a note at the top:
   - "The wrapper has computed a HEALTH SNAPSHOT in the runtime context block above. USE IT as your primary signal. Brain reports and SQLite queries should be a follow-up to verify or zoom in on what the snapshot shows."
4. `scripts/dev_loop_weekly_prompt.md` Step 1 gets the same note.
5. The snapshot is computed unconditionally on every run (live, dry, force).
6. If `Get-BotHealthSnapshot` returns `$null`, the runtime context block reads `## HEALTH SNAPSHOT (unavailable)` and the LLM falls back to the existing observe procedure.
7. Wrapper still parses cleanly via PSParser tokenization.
8. After the patch, fire `pwsh -File scripts/dev_loop.ps1 -DryRun -Force` and verify:
   - "computed health snapshot: trades_7d=N pnl_7d=$X.XX" log line
   - The runtime context block in the run log file contains the HEALTH SNAPSHOT section with non-null values

## Non-goals

- Do not cache the snapshot across runs (it's cheap to compute, no point caching)
- Do not add new SQLite columns or indexes
- Do not touch the bot's code -- this is wrapper-only
- Do not change the YAML output format
- Do not add a separate budget gate based on snapshot values (let the LLM decide)
- Do not include per-pair details (trade-level data is what brain reports are for)

## Files in scope

- `scripts/dev_loop.ps1`
- `scripts/dev_loop_prompt.md` (Step 1 note)
- `scripts/dev_loop_weekly_prompt.md` (Step 1 note)
- `tasks/specs/22-dev-loop-health-snapshot.result.md`

## Cost note

The snapshot is one SQLite query batch + one curl. Adds maybe 50ms to wrapper startup. Saves the LLM significant input tokens by precomputing what it would otherwise have to derive.

## Evidence

- Today's live run (state/dev-loop/runs/20260412_210639.log): 301k input tokens to read 12 brain reports + query SQLite. With a precomputed snapshot, the LLM should need to read FEWER brain reports unless it wants to investigate something the snapshot flagged.
- Spec 19 weekly prompt has "win rate dropped > 10pp week-over-week" as a priority but the LLM has to compute it from raw data each run.
