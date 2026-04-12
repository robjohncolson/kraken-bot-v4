# Spec 13 result — parallel runner stale worktree cleanup

Implemented in the Agent repo (`C:/Users/rober/Downloads/Projects/Agent`).

## Files changed

- `runner/parallel-codex-runner.py` — added `_force_remove_dir()` helper at module-level (line 159) and replaced the cleanup block in `_prepare_agent_worktree()` (line 720) to call it instead of the previous `shutil.rmtree(..., ignore_errors=True)` pattern. +50/-1 lines.
- `runner/test_parallel_runner_cleanup.py` — new test file with 4 tests.

## Behavior

The new `_force_remove_dir(target, log_handle, max_retries=4, retry_delay=1.0)` helper:
- No-ops when the target doesn't exist
- Calls `shutil.rmtree` with an `onerror` callback that chmods read-only files to writable and retries the failing operation
- If the rmtree itself raises, retries the whole operation up to `max_retries` times with `retry_delay` seconds between attempts
- On final failure, raises a `RunnerError` with the path and underlying exception, plus an actionable "Manual cleanup required: rm -rf <path>" hint
- Logs each retry attempt to the supplied log handle

## Test results

All 4 tests pass (verified manually):

```
runner/test_parallel_runner_cleanup.py::test_force_remove_dir_removes_normal_tree PASSED
runner/test_parallel_runner_cleanup.py::test_force_remove_dir_removes_tree_with_readonly_file PASSED
runner/test_parallel_runner_cleanup.py::test_force_remove_dir_missing_path_is_noop PASSED
runner/test_parallel_runner_cleanup.py::test_force_remove_dir_raises_runner_error_on_persistent_failure PASSED
4 passed in 0.07s
```

## Validation

- Read-only file case: covered by `test_force_remove_dir_removes_tree_with_readonly_file`
- Persistent-failure case: covered by `test_force_remove_dir_raises_runner_error_on_persistent_failure` (mocks `shutil.rmtree` to always raise PermissionError, asserts RunnerError with "Manual cleanup required" in message)
- No-op case: covered by `test_force_remove_dir_missing_path_is_noop`
- Normal tree: covered by `test_force_remove_dir_removes_normal_tree`

## Follow-up

None. Independent of specs 11 and 12. Ready to merge into the Agent repo.
