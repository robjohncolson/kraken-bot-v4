# Spec 24 -- Bot persists reconciliation discrepancies as cc_memory entries

## Problem

The CC orchestrator's health snapshot (spec 22+23) tracks `recon_errors_24h` from `cc_memory.category = 'reconciliation_anomaly'`. The orchestrator's tactical priority list (rule 5) also fires on "reconciliation discrepancy logged >= 3 times in 24h". But the bot **never writes** that memory category. Reconciliation warnings are only logged to stdout/file, not persisted.

Result:
- The snapshot's `recon_errors_24h` is always 0
- The orchestrator's priority rule 5 can never fire
- The recurring `untracked_assets=4-5` warning has been visible in brain logs for days but invisible to the orchestrator's structured signals

## Desired outcome

When `runtime_loop.py` logs a reconciliation discrepancy warning, it ALSO writes a `cc_memory` row with `category='reconciliation_anomaly'`, the discrepancy summary, and a timestamp. The orchestrator's snapshot then sees real recon error counts and can act on them.

## Acceptance criteria

1. `runtime_loop.py:_handle_effects()` (around line 959-980 where `ReconciliationDiscrepancy` events are handled) gains a write to `cc_memory` after logging the warning. The memory entry should:
   - `category='reconciliation_anomaly'`
   - `pair=null` (it's not pair-specific)
   - `content` = JSON with the same fields the existing summary has: `ghost_positions, foreign_orders, fee_drift, untracked_assets`. Optionally include the list of untracked asset symbols if available from the `ReconciliationReport`.
   - `importance=0.7` (medium-high)
2. The write goes through the existing `cc_memory` write path (probably `persistence/cc_memory.py:write()` or similar). Use whatever pattern other categories already use.
3. **Deduplication**: don't write a new memory if the most recent `reconciliation_anomaly` memory has the SAME content payload AND was written < 5 minutes ago. This prevents flooding the table when the bot is in a steady state with persistent untracked assets.
4. Add a unit test in `tests/test_runtime_loop.py`:
   - Construct a fake `ReconciliationDiscrepancy` event with `untracked_assets=['FLOW']`
   - Run it through the handler
   - Assert that `cc_memory` got a row with the right category and content
   - Run the same event again immediately, assert NO new memory (dedupe)
   - Advance the mock clock 6 minutes, run again, assert NEW memory (dedupe expired)
5. Full pytest suite green (`python -m pytest tests/ -x`).

## Non-goals

- Do not change the format of the existing log warning. Just add the memory write alongside.
- Do not retroactively backfill `reconciliation_anomaly` memories for past discrepancies.
- Do not change the reconciler logic itself. The reducer is correct.
- Do not address the underlying "FLOW/HYPE/MON/TRIA are untracked" issue (that's spec 16's domain for new orders, plus a separate cleanup spec for legacy residuals).
- Do not modify the orchestrator wrapper -- it's already reading the right category.

## Files in scope

- `runtime_loop.py` (or wherever `ReconciliationDiscrepancy` is handled)
- `persistence/cc_memory.py` (only if a new helper is needed -- prefer reusing existing)
- `tests/test_runtime_loop.py`
- `tasks/specs/24-bot-persist-recon-anomalies.result.md`

## Evidence

- `state/cc-reviews/brain_2026-04-12_*.md` reports show "Reducer: reconciliation: ... untracked_assets=4" / "WARNING runtime_loop Reconciliation discrepancy detected: ... untracked_assets=4" on every cycle
- `data/bot.db` `cc_memory` query: `SELECT category, COUNT(*) FROM cc_memory WHERE timestamp > datetime('now','-24 hours') GROUP BY category` returns no `reconciliation_anomaly` rows
- The orchestrator's priority list (`scripts/dev_loop_prompt.md` Step 2 rule 5) explicitly says "Reconciliation discrepancy logged >= 3 times in 24h -- state-machine drift" -- this rule can never fire today
