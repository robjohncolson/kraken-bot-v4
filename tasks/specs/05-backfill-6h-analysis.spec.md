# Spec 05 — Backfill analysis at 6h forward window

## Problem

The first historical backfill run used a 2-hour forward window
because only ~4 hours of reports existed post-scoring-model-update.
Result: 6 evaluable cycles, 6/6 shadow-over-live, but sample was
small and clustered within a single 40-minute slice with duplicate
decisions. The 6h window from the promotion criterion has never
been exercised.

A full night of scheduled 2h cycles has now elapsed (~8-10 new
brain reports). Enough time has passed for the 6h forward window
to become available on multiple historically-independent decision
points.

## Desired outcome

Quantitative evidence — not anecdote — on whether the unified
(shadow) path would have outperformed live's pair-specific picks
over a 6h forward window across enough cycles to matter.

## Acceptance criteria

1. Run `python scripts/backfill_shadow.py --forward-hours 6` and
   capture the output.
2. At least 8 cycles must be evaluable with the 6h window (each
   cycle must be > 7 hours old so we have the forward bar + buffer).
3. Compute:
   - Shadow-win rate (shadow ret > live ret by at least 0.1%)
   - Live-win rate
   - Tie rate (within ±0.1%)
   - Cumulative difference (sum of `shadow_ret - live_ret`)
   - Average per-cycle edge
4. Produce a written analysis in
   `tasks/specs/05-backfill-6h-analysis.result.md` covering:
   - Raw numbers and per-cycle table
   - Whether the 6/6 short-window result held at the longer window
   - Whether the sample-size caveats from the first run are
     materially improved (n ≥ 8, multiple independent decisions)
   - Recommendation: promote the shadow path to full driver,
     keep the narrow veto, or extend to the hybrid design in
     spec 04
5. If the analysis reveals a bias or edge case that the existing
   aggregation doesn't handle, document it as a follow-up spec
   (do not fix it in this task).

## Non-goals

- Do not modify the backfill script's scoring logic unless a
  genuine bug is found.
- Do not place any live orders based on the analysis. This is
  a read-only diagnostic.
- Do not redesign `compute_unified_holds` as part of this task.
- Do not touch `check_exits`, `evaluate_portfolio`, or any other
  live decision code.

## Evidence/inputs

- `state/cc-reviews/brain_2026-04-11_2323.md` through
  `brain_2026-04-12_1210.md` — ~20 reports across a 13-hour window
  at time of writing
- `scripts/backfill_shadow.py` — the existing tool
- Earlier result: 6/6 shadow-win at 2h window, 6 clustered cycles,
  cumulative live −17.80%
