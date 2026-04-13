# Spec 31 -- Dedupe rotation_tree_drift cc_memory writes + log

## Problem

`_record_rotation_tree_drift` in `runtime_loop.py:1188-1231` writes a `cc_memory` row and a `logger.warning` every time the rotation tree value is computed and the drift exceeds tolerance. Because `_compute_rotation_tree_value_details` runs on every valuation cycle (once per runtime tick + once per API hit), the function fires very frequently.

Live bot observation immediately after spec 29 landed: **7 `rotation_tree_drift` memory rows in the first hour**, rate ~1 per 19 seconds of uptime, unbounded. Rate projected to **~4500 rows/day**, all with nearly identical content (same 7 roots, same delta, different timestamp). The `cc_memory` table and the log would fill with useless duplicates in days.

Spec 24 already solved exactly this problem for `reconciliation_anomaly`: instance-level last-content/last-timestamp vars, dedupe on identical frozen content within a 5-minute window. Apply the same pattern here.

## Desired outcome

1. `rotation_tree_drift` memory rows are written at most once per 5 minutes for identical drift content.
2. When the drift signature changes (different roots, substantially different delta), a new row is written immediately.
3. The log warning is rate-limited by the same dedupe check (so stdout stops spamming too).
4. Existing `rotation_tree_drift` writing behavior on first observation is preserved -- the first hit of a new drift signature always writes.
5. Cc_memory spam rate drops from ~4500/day to O(drift-state-changes/day), which should be a handful at most.

## Acceptance criteria

1. **Add dedupe state to `SchedulerRuntime.__init__`** (or wherever `_last_recon_anomaly_content` and `_last_recon_anomaly_ts` are initialized):
   ```python
   self._last_rotation_tree_drift_content: str | None = None
   self._last_rotation_tree_drift_ts: datetime | None = None
   ```
   Location: look for the existing `_last_recon_anomaly_content` declaration around line 493-494 and add the two new fields next to them.

2. **Add a module-level constant** for the dedupe window alongside any existing recon dedupe constant:
   ```python
   ROTATION_TREE_DRIFT_DEDUPE_WINDOW = timedelta(minutes=5)
   ```
   If the recon window is already named something like `RECON_ANOMALY_DEDUPE_WINDOW`, match that naming.

3. **Apply dedupe in `_record_rotation_tree_drift`** at `runtime_loop.py:1188`:
   - Build the `content` dict as today.
   - Compute a frozen signature: `frozen_content = json.dumps(content, sort_keys=True)`.
   - Compute `now` from `datetime.now(timezone.utc)` (or match whatever the recon path uses).
   - If `frozen_content == self._last_rotation_tree_drift_content AND self._last_rotation_tree_drift_ts is not None AND now - self._last_rotation_tree_drift_ts < ROTATION_TREE_DRIFT_DEDUPE_WINDOW`, return early (no log, no memory write).
   - Otherwise: proceed with the existing `logger.warning(...)` and `self._cc_memory._write(...)` calls, then update `self._last_rotation_tree_drift_content = frozen_content` and `self._last_rotation_tree_drift_ts = now`.

4. **The "return early" path must be silent** -- no log output at all. Spam reduction is the entire point. If debugging is ever needed, the content-change path will re-emit.

5. **Tests** in `tests/test_runtime_loop.py`:
   - `test_rotation_tree_drift_memory_deduped_within_window`: call `_record_rotation_tree_drift` twice in a row with identical content; assert the `cc_memory._write` mock was called exactly once.
   - `test_rotation_tree_drift_memory_rewritten_after_window`: call twice, but freeze `datetime.now` so the second call is > 5 min after the first; assert 2 writes.
   - `test_rotation_tree_drift_memory_rewritten_on_content_change`: call twice with different `delta_usd` (e.g. change tree_total by $10); assert 2 writes.
   - `test_rotation_tree_drift_log_also_rate_limited`: use `caplog` to assert the duplicated call produces zero log records (the first call still logs).

6. Full pytest green (should be 708 + 4 = 712 passing).

## Non-goals

- Do not change the tolerance threshold (`max($1, 0.5%)`). The drift detection itself is correct; only the write rate is wrong.
- Do not rewrite the historical 7 `rotation_tree_drift` memory rows already in SQLite. They stay as historical record.
- Do not touch `_record_reconciliation_anomaly`. Its dedupe is already working and is the reference pattern.
- Do not add a new config knob for the window. 5 minutes, hard-coded, matches spec 24.
- Do not attempt to restart the bot as part of this spec. The parent agent will handle restart after commit.

## Files in scope

- `runtime_loop.py` (new state fields, constant, dedupe logic)
- `tests/test_runtime_loop.py` (four new tests)
- `tasks/specs/31-drift-memory-dedupe.result.md`

## Evidence

- Spec 29 result: spec 29's hypothesis was backwards, pruner is dormant, but the drift-warning side effect is writing memory rows at ~1/19s.
- Bot log excerpt from `state/scheduled-logs/main_restart_20260413_230926.log` shows three drift warnings within 2 seconds at startup, and continued firing every cycle.
- Live `/api/memory?category=rotation_tree_drift&hours=1` returned 7 rows in the first hour post-restart.
- Spec 24 reference: `runtime_loop.py:1022,1027-1054` for the `reconciliation_anomaly` dedupe pattern. Instance vars at line 493-494.
