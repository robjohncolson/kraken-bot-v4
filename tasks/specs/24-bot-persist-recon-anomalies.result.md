Implemented Spec 24 in the bot runtime and tests.

- `runtime_loop.py`
  - Added `CCMemory` usage on the existing SQLite connection.
  - Persisted `reconciliation_anomaly` memory rows from `_handle_effects()` when `ReconciliationDiscrepancy` effects are logged.
  - Stored payload fields: `ghost_positions`, `foreign_orders`, `fee_drift`, `untracked_assets`, and `untracked_asset_symbols`.
  - Added in-memory dedupe using the last payload plus timestamp, suppressing identical writes for 5 minutes.

- `tests/test_runtime_loop.py`
  - Added coverage for initial persistence to `cc_memory`.
  - Added coverage for dedupe suppression on immediate duplicate discrepancies.
  - Added coverage for writing again after the 5 minute dedupe window expires.

- Verification
  - Per subagent instructions, I did not run `python -m pytest tests/test_runtime_loop.py -x`.
  - GitNexus `impact` and `context` calls for `_handle_effects` were attempted twice but the MCP returned `user cancelled MCP tool call`, so I proceeded with direct code inspection instead.
