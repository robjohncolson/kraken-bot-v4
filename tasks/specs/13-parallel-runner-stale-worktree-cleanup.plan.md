# Plan — Spec 13: parallel runner cleanup robustness

## Context for the implementer

Read first: `tasks/specs/13-parallel-runner-stale-worktree-cleanup.spec.md`

## Working directory

**This spec targets the Agent repo at
`C:/Users/rober/Downloads/Projects/Agent`, NOT kraken-bot-v4.**

## Where the fix goes

Single function: `_prepare_agent_worktree()` in
`runner/parallel-codex-runner.py`, around line 658.

## Implementation approach

### Step 1 — Extract a helper

Add a module-level helper:

```python
def _force_remove_dir(target: pathlib.Path, log_handle: Any | None = None,
                     max_retries: int = 4, retry_delay: float = 1.0) -> None:
    """Remove a directory tree, retrying on Windows file-lock errors.

    Raises RunnerError on final failure with a clear message.
    """
    import errno
    import stat
    import time

    if not target.exists():
        return

    def _on_rm_error(func, path, exc_info):
        # Clear read-only flag and retry
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            func(path)
        except Exception:
            raise

    last_exc: Exception | None = None
    for attempt in range(max_retries):
        try:
            shutil.rmtree(target, onerror=_on_rm_error)
            return
        except (PermissionError, OSError) as exc:
            last_exc = exc
            if log_handle is not None:
                log_handle.write(
                    f"[cleanup] attempt {attempt + 1}/{max_retries} failed: {exc}\n"
                )
            time.sleep(retry_delay)

    raise RunnerError(
        f"Failed to remove stale worktree directory after {max_retries} attempts: "
        f"{target} ({last_exc}). Manual cleanup required: rm -rf '{target}'"
    )
```

### Step 2 — Use it in `_prepare_agent_worktree`

Replace lines 664-671:

```python
if worktree_dir.exists():
    # Try git's own cleanup first
    self._run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        cwd=self.repo_root,
        check=False,
    )
    # If git's removal didn't fully clean up, force-remove with retries
    if worktree_dir.exists():
        _force_remove_dir(worktree_dir, log_handle=log_handle)
```

### Step 3 — Test

Add `runner/test_parallel_runner_cleanup.py` (or extend whatever
test infra exists). Tests:

```python
def test_force_remove_dir_clean_path(tmp_path):
    # Create a normal dir with files, remove it cleanly
    target = tmp_path / "worktree"
    target.mkdir()
    (target / "file.txt").write_text("hi")
    _force_remove_dir(target)
    assert not target.exists()

def test_force_remove_dir_readonly_files(tmp_path):
    # Create a dir with a read-only file (the common Windows case)
    target = tmp_path / "worktree"
    target.mkdir()
    f = target / "locked.txt"
    f.write_text("hi")
    os.chmod(f, stat.S_IREAD)  # remove write bit
    _force_remove_dir(target)
    assert not target.exists()

def test_force_remove_dir_nonexistent(tmp_path):
    # No-op on missing dir
    _force_remove_dir(tmp_path / "nope")  # should not raise

def test_force_remove_dir_raises_on_persistent_failure(tmp_path, monkeypatch):
    # Mock shutil.rmtree to always raise PermissionError
    # Verify _force_remove_dir raises RunnerError after max_retries
    target = tmp_path / "worktree"
    target.mkdir()
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(RunnerError, match="Manual cleanup required"):
        _force_remove_dir(target, max_retries=2, retry_delay=0.01)
```

### Step 4 — Validate

1. Run the new tests: `pytest runner/test_parallel_runner_cleanup.py -x`
2. Manual test: create a fake worktree dir with a `pytest-cache-files-*`
   subfolder, run a single-agent dispatch via the runner, confirm
   the dir is cleaned up before the new worktree is added.
3. Smoke-test the runner against an existing manifest (e.g., a
   no-op single-agent manifest) to confirm clean runs are unaffected.

## Files to modify

- `runner/parallel-codex-runner.py` — add helper, replace cleanup
- `runner/test_parallel_runner_cleanup.py` — new test file (or
  extend existing tests)

## Dependencies

None. Independent of specs 11 and 12.

## Risk

LOW. The helper is more strict than the existing code (loud failure
instead of silent), which is the desired behavior. Clean-state runs
are unaffected. The retry logic is bounded (4 attempts × 1s = 4s
worst case). The chmod-on-error fallback handles read-only files
without breaking anything.
