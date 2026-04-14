# Plan 33 -- Widen reconciliation/drift memory dedupe windows

## Verified root cause

Lines 117-118 of `runtime_loop.py`:
```python
RECONCILIATION_ANOMALY_DEDUPE_WINDOW = timedelta(minutes=5)
ROTATION_TREE_DRIFT_DEDUPE_WINDOW = timedelta(minutes=5)
```

These are too short relative to the 30s cycle interval, producing ~12 writes/hour
for unchanging content.

## Implementation steps

### Step 1: Change constants (runtime_loop.py)

Line 117: `timedelta(minutes=5)` -> `timedelta(minutes=60)`
Line 118: `timedelta(minutes=5)` -> `timedelta(minutes=30)`

### Step 2: Update reconciliation anomaly dedupe tests (tests/test_runtime_loop.py)

Find `test_recon_discrepancy_dedupe_within_5min`:
- The test advances time by some amount < 5 minutes and checks that the second
  write is suppressed. Update the time advance and any comments/names to reflect
  the 60-minute window. The within-window advance should be < 60 minutes.

Find `test_recon_discrepancy_writes_again_after_dedupe_window`:
- The test advances time past the dedupe window and checks that the second write
  goes through. Update to advance > 60 minutes.

### Step 3: Update rotation_tree_drift dedupe tests (tests/test_runtime_loop.py)

Find `test_rotation_tree_drift_memory_deduped_within_window`:
- Update time advance to < 30 minutes.

Find `test_rotation_tree_drift_memory_rewritten_after_window`:
- Update time advance to > 30 minutes.

Find `test_rotation_tree_drift_log_also_rate_limited`:
- Update time advance to < 30 minutes.

### Step 4: Run pytest

`C:/Python313/python.exe -m pytest tests/ -x --tb=short -q`

All tests must pass.

## Owned paths

- `runtime_loop.py`
- `tests/test_runtime_loop.py`
