# Spec 13 — Parallel runner stale worktree cleanup robustness

## Problem

`Agent/runner/parallel-codex-runner.py` already has cleanup logic in
`_prepare_agent_worktree()` (lines 658-711) that removes the
worktree dir before re-adding it:

```python
if worktree_dir.exists():
    self._run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        cwd=self.repo_root,
        check=False,
    )
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)
```

But this still **fails in practice** on Windows when prior runs
leave behind `pytest-cache-files-*` directories with file locks.
Observed during the kraken-bot hardening batch: the
`02-open-orders` agent left an orphaned dir, the runner's cleanup
silently swallowed `Permission denied` errors via
`ignore_errors=True`, and the next attempt failed when
`git worktree add --force` couldn't write into a non-empty path.

Manual recovery required: `rm -rf state/parallel-worktrees/02-open-orders`
followed by re-dispatch.

## Desired outcome

Stale worktree directories from prior runs are reliably cleaned up
on retry, even when Windows file locks are present. If cleanup
genuinely cannot proceed, the runner fails loudly with an actionable
error message instead of silently leaving a half-cleaned directory.

## Acceptance criteria

1. `_prepare_agent_worktree()` no longer uses `ignore_errors=True`
   without a fallback. If `shutil.rmtree` fails:
   - Log the specific OSError + path
   - Retry up to N times with brief delay (Windows file locks
     usually clear within seconds)
   - On final failure, raise `RunnerError` with a message that
     includes the path and the underlying error so the user knows
     exactly what to delete manually
2. The retry uses an OS-aware approach. On Windows, file locks
   commonly come from antivirus / Windows Defender / pytest cache
   files. A `time.sleep(1.0)` between retries is sufficient in most
   cases.
3. A specific helper handles the
   `state/parallel-worktrees/<agent>/state/pytest-temp` and
   `pytest-cache-files-*` cases, since those are the observed
   offenders. One option: `chmod -R u+w` (or equivalent on Windows)
   before rmtree to clear any read-only flags.
4. A unit/integration test reproduces the failure with a synthetic
   read-only file inside a fake worktree dir, then verifies the
   cleanup helper succeeds (or fails loudly with the new error
   message) without `ignore_errors=True` masking it.
5. The change is backwards-compatible: a clean run with no stale
   worktree behaves identically to the current code.

## Non-goals

- Do not redesign the worktree-per-agent architecture.
- Do not switch from `git worktree` to clone-per-agent.
- Do not add a CLI flag to disable cleanup (the runner already has
  `--reset` which forces a state reset; this spec is about making
  the always-run cleanup robust).
- Do not change the manifest schema or add new agent-level config.

## Affected files

- `Agent/runner/parallel-codex-runner.py` — primary fix
- `Agent/runner/test_parallel_runner_cleanup.py` — new test file
  (or extend existing test scaffolding if present)

## Working directory for Codex

This spec targets the **Agent repo**, not kraken-bot-v4. Codex must
be dispatched with `--working-dir C:/Users/rober/Downloads/Projects/Agent`
and `--owned-paths runner/parallel-codex-runner.py runner/test_*.py`.

## Evidence

- `Agent/runner/parallel-codex-runner.py` lines 658-711
- CONTINUATION_PROMPT.md (kraken-bot-v4) "Known runner quirks": item 1
  ("Orphaned worktree dirs from failed runs block retries. Manual
  rm -rf state/parallel-worktrees/<agent> required.")
