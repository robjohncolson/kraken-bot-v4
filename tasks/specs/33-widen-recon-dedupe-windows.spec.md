# Spec 33 -- Widen reconciliation/drift memory dedupe windows

## Problem

Spec 24 added a 5-minute dedupe window for `reconciliation_anomaly` writes.
Spec 31 added a 5-minute structural dedupe window for `rotation_tree_drift` writes.

Both windows are too short. The runtime loop cycles every 30 seconds, so entries
are written once per ~5 minutes (the window boundary). Over the last 7 hours
since the last code commit, the bot has written:

- **178** `rotation_tree_drift` entries (~25/hour)
- **85** `reconciliation_anomaly` entries (~12/hour)

The reconciliation_anomaly content is **identical every single time**:
`{"ghost_positions":0,"foreign_orders":0,"fee_drift":0,"untracked_assets":2,"untracked_asset_symbols":["FLOW","TRIA"]}`

The rotation_tree_drift structural signature (root IDs, missing prices,
pruned count) is also identical when the tree structure hasn't changed.

This inflates the health snapshot's "Recon errors 24h" counter to ~283,
drowning real anomalies in noise and making the dev-loop's priority-5 rule
("reconciliation discrepancy >= 3 times in 24h") fire on benign repeats.

## Root cause

`RECONCILIATION_ANOMALY_DEDUPE_WINDOW` and `ROTATION_TREE_DRIFT_DEDUPE_WINDOW`
are both `timedelta(minutes=5)` (runtime_loop.py:117-118). With a 30-second
cycle interval, entries at exactly 5 minutes apart pass the `< DEDUPE_WINDOW`
check, so the effective suppression is ~10 cycles = 5 min, yielding 12
writes/hour for unchanging content.

## Desired outcome

1. `reconciliation_anomaly` with identical content writes at most once per
   60 minutes (down from 12/hour to ~1/hour).
2. `rotation_tree_drift` with identical structural signature writes at most
   once per 30 minutes (down from 12/hour to ~2/hour).
3. Both still write immediately when content/signature actually changes.
4. Health snapshot "Recon errors 24h" drops from ~283 to ~48 for the same
   underlying state (still non-zero, but manageable noise).

## Acceptance criteria

1. Change `RECONCILIATION_ANOMALY_DEDUPE_WINDOW` from `timedelta(minutes=5)`
   to `timedelta(minutes=60)` at runtime_loop.py:117.

2. Change `ROTATION_TREE_DRIFT_DEDUPE_WINDOW` from `timedelta(minutes=5)`
   to `timedelta(minutes=30)` at runtime_loop.py:118.

3. Update existing test `test_recon_discrepancy_dedupe_within_5min` in
   tests/test_runtime_loop.py to use the new 60-minute window (the test
   name may need updating too).

4. Update existing test `test_recon_discrepancy_writes_again_after_dedupe_window`
   to use the new 60-minute window.

5. Update existing tests for rotation_tree_drift dedupe
   (`test_rotation_tree_drift_memory_deduped_within_window`,
   `test_rotation_tree_drift_memory_rewritten_after_window`,
   `test_rotation_tree_drift_log_also_rate_limited`) to use the new
   30-minute window.

6. All 716+ existing tests still pass.

## Evidence

```
# rotation_tree_drift per hour today (since midnight UTC)
hour 00: 91  (3 restarts in this hour, each resets in-memory state)
hour 01: 12
hour 02: 12
hour 03: 12
hour 04: 13
hour 05: 12
hour 06: 12
hour 07: 5

# reconciliation_anomaly since last commit: 85 entries, all identical content
```

## Owned paths

- `runtime_loop.py` (lines 117-118 only -- the two constants)
- `tests/test_runtime_loop.py` (dedupe-related tests only)
