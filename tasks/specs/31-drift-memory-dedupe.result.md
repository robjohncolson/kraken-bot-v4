Implemented Spec 31 in the runtime loop and tests.

- `runtime_loop.py`
  - Added `ROTATION_TREE_DRIFT_DEDUPE_WINDOW = timedelta(minutes=5)`.
  - Added instance-local dedupe state for the last frozen `rotation_tree_drift` payload and timestamp next to the existing reconciliation anomaly state.
  - Updated `_record_rotation_tree_drift()` to freeze the payload, suppress identical writes/logs inside the 5 minute window, and still write immediately when the drift content changes or the window expires.

- `tests/test_runtime_loop.py`
  - Added coverage for duplicate suppression within the dedupe window.
  - Added coverage for writing again after the dedupe window expires.
  - Added coverage for writing again when the drift payload changes.
  - Added coverage that the warning log is rate-limited by the same silent early-return path.

- Verification
  - Per subagent instructions, I did not run pytest, lint, or any other verification commands.
  - GitNexus `impact`/`context` calls were attempted before editing, but the MCP returned `user cancelled MCP tool call` in this subagent context, so blast radius was checked manually from the local call sites instead.
