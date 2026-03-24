# Build Orchestration Design

## Purpose

`build/` is the repo-local orchestration layer that lets Claude Code act as planner/reviewer and Codex act as scoped implementer while building `kraken-bot-v4` from [SPEC.md](/C:/Users/ColsonR/kraken-bot-v4/SPEC.md).

V1 is deliberately narrow:

- Decomposition is manual and done by Claude Code.
- Codex is only used for scoped implementation/correction passes.
- Verification is targeted `pytest` plus Claude Code diff review.
- One commit is created per atomic task.
- No auto-push.
- No Fractals runtime dependency.
- No unattended autonomous review loop.

The design below keeps deterministic work in Python and keeps semantic code review in Claude Code, because that is the smallest system that matches the actual tooling.

## Non-Goals

- No automatic phase decomposition.
- No git worktree orchestration in v1.
- No automatic merge agent.
- No codex-to-cc nested delegation inside a Codex implementation call.
- No commit/push without an explicit Claude Code approval step.

## File Tree

```text
build/
├── BUILD_SYSTEM_DESIGN.md             # This document
├── manifests/                         # Tracked, human-authored plans
│   ├── phase-1.foundation.json
│   ├── phase-2.grid.json
│   └── ...
├── schemas/                           # Tracked JSON schemas (documentation + runtime validation source)
│   ├── task-manifest.schema.json
│   └── phase-state.schema.json
├── templates/                         # Tracked prompt/summary templates
│   ├── codex-task-prompt.md
│   ├── codex-correction-prompt.md
│   └── phase-summary.md
├── state/                             # Untracked mutable build state
│   ├── phase-1.foundation.state.json
│   ├── phase-2.grid.state.json
│   └── tasks/
│       └── 1.1/
│           └── attempt-01/
│               ├── implement.prompt.md
│               ├── correction.prompt.md
│               ├── cross-agent.request.json
│               ├── cross-agent.result.json
│               ├── codex.stdout.json
│               ├── codex.stderr.txt
│               ├── pytest.txt
│               ├── diff.patch
│               ├── review.md
│               ├── review.decision.json
│               └── commit.txt
├── logs/                              # Untracked operational logs
│   └── build-loop.ndjson
├── run_phase.py                       # Main controller, < 500 lines
└── common.py                          # Small stdlib helpers (atomic JSON, git, subprocess, time)
```

## Tracking Rules

Tracked:

- `build/manifests/`
- `build/schemas/`
- `build/templates/`
- `build/BUILD_SYSTEM_DESIGN.md`

Untracked / gitignored:

- `build/state/`
- `build/logs/`
- repo-root `state/cross-agent/` created by the shared cross-agent runner

Recommended `.gitignore` additions when implementation starts:

```gitignore
build/state/
build/logs/
state/
```

## Task Manifest Format

The manifest is the immutable phase plan. Claude Code writes it once when a phase is decomposed. Runtime status does not live here.

One manifest file per SPEC phase:

- `build/manifests/phase-1.foundation.json`
- `build/manifests/phase-2.grid.json`

### `task-manifest.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "build/schemas/task-manifest.schema.json",
  "title": "Build Task Manifest",
  "type": "object",
  "required": [
    "schema_version",
    "project",
    "phase",
    "global_constraints",
    "tasks"
  ],
  "properties": {
    "schema_version": {
      "type": "string",
      "const": "build-manifest/v1"
    },
    "project": {
      "type": "string",
      "const": "kraken-bot-v4"
    },
    "phase": {
      "type": "object",
      "required": ["id", "name", "spec_ref", "created_at", "created_by"],
      "properties": {
        "id": { "type": "string", "pattern": "^[1-6]$" },
        "name": { "type": "string" },
        "spec_ref": { "type": "string" },
        "created_at": { "type": "string" },
        "created_by": { "type": "string" }
      },
      "additionalProperties": false
    },
    "global_constraints": {
      "type": "object",
      "required": [
        "max_attempts_per_task",
        "max_files_changed_per_task",
        "module_line_cap",
        "typed_exceptions_only",
        "frozen_dataclasses_preferred",
        "no_git_push"
      ],
      "properties": {
        "max_attempts_per_task": { "type": "integer", "minimum": 1 },
        "max_files_changed_per_task": { "type": "integer", "minimum": 1 },
        "module_line_cap": { "type": "integer", "minimum": 100 },
        "typed_exceptions_only": { "type": "boolean" },
        "frozen_dataclasses_preferred": { "type": "boolean" },
        "no_git_push": { "type": "boolean" }
      },
      "additionalProperties": false
    },
    "tasks": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": [
          "id",
          "description",
          "acceptance_criteria",
          "owned_paths",
          "dependencies",
          "status",
          "test_targets"
        ],
        "properties": {
          "id": {
            "type": "string",
            "pattern": "^[1-6](\\.[0-9]+)+$"
          },
          "description": { "type": "string", "minLength": 1 },
          "acceptance_criteria": {
            "type": "array",
            "minItems": 1,
            "items": { "type": "string" }
          },
          "owned_paths": {
            "type": "array",
            "minItems": 1,
            "items": { "type": "string" }
          },
          "dependencies": {
            "type": "array",
            "items": { "type": "string" }
          },
          "status": {
            "type": "string",
            "enum": [
              "pending",
              "in-progress",
              "verifying",
              "correcting",
              "done",
              "failed",
              "blocked"
            ]
          },
          "test_targets": {
            "type": "array",
            "items": { "type": "string" }
          },
          "notes": { "type": "string" }
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

### Manifest Design Notes

- `status` exists in the manifest only for human readability at creation time. Runtime truth lives in phase state.
- `owned_paths` must fit inside the cross-agent runner's hard-coded `max_files_changed = 10` behavior, so any task expected to touch more than 10 files must be decomposed further.
- `test_targets` may be empty for the first task that creates the test file itself, but the expected end state should still name the future target.

## Phase State Format

The phase state is the mutable execution ledger. It is updated after every durable step and is the only file the controller trusts for resume.

### `phase-state.schema.json`

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "build/schemas/phase-state.schema.json",
  "title": "Build Phase State",
  "type": "object",
  "required": [
    "schema_version",
    "manifest_path",
    "phase_id",
    "phase_name",
    "phase_status",
    "updated_at",
    "tasks"
  ],
  "properties": {
    "schema_version": {
      "type": "string",
      "const": "build-state/v1"
    },
    "manifest_path": { "type": "string" },
    "phase_id": { "type": "string" },
    "phase_name": { "type": "string" },
    "phase_status": {
      "type": "string",
      "enum": ["pending", "active", "awaiting-review", "done", "failed", "blocked"]
    },
    "current_task_id": {
      "type": ["string", "null"]
    },
    "updated_at": { "type": "string" },
    "tasks": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": [
          "status",
          "attempt_count",
          "resume_from",
          "attempts"
        ],
        "properties": {
          "status": {
            "type": "string",
            "enum": [
              "pending",
              "in-progress",
              "verifying",
              "correcting",
              "done",
              "failed",
              "blocked"
            ]
          },
          "attempt_count": { "type": "integer", "minimum": 0 },
          "resume_from": {
            "type": "string",
            "enum": [
              "start",
              "invoke-subagent",
              "pytest",
              "cc-review",
              "commit",
              "complete"
            ]
          },
          "last_error": {
            "type": ["string", "null"]
          },
          "commit_sha": {
            "type": ["string", "null"]
          },
          "attempts": {
            "type": "array",
            "items": {
              "type": "object",
              "required": [
                "attempt",
                "mode",
                "started_at",
                "artifacts_dir"
              ],
              "properties": {
                "attempt": { "type": "integer", "minimum": 1 },
                "mode": {
                  "type": "string",
                  "enum": ["implement", "correct"]
                },
                "started_at": { "type": "string" },
                "finished_at": { "type": ["string", "null"] },
                "call_id": { "type": ["string", "null"] },
                "artifacts_dir": { "type": "string" },
                "changed_files": {
                  "type": "array",
                  "items": { "type": "string" }
                },
                "pytest_exit_code": {
                  "type": ["integer", "null"]
                },
                "review_decision": {
                  "type": ["string", "null"],
                  "enum": ["approve", "correct", "fail", null]
                },
                "review_notes_path": {
                  "type": ["string", "null"]
                }
              },
              "additionalProperties": false
            }
          }
        },
        "additionalProperties": false
      }
    }
  },
  "additionalProperties": false
}
```

## State File Behavior

### Canonical Locations

- Manifest: `build/manifests/phase-N.slug.json`
- Mutable phase state: `build/state/phase-N.slug.state.json`
- Per-attempt artifacts: `build/state/tasks/<task-id>/attempt-0N/`
- Cross-agent raw request/result: repo-root `state/cross-agent/<call_id>.request.json|result.json`
- Event log: `build/logs/build-loop.ndjson`

### Update Strategy

- Every state write is atomic: write `*.tmp`, then `os.replace`.
- State updates happen after each durable milestone:
  - selected task
  - prompt written
  - Codex invocation finished
  - pytest finished
  - review decision recorded
  - commit created

### Resume Rules

On startup, the controller loads the phase state and inspects any nonterminal task:

1. If `resume_from == "invoke-subagent"` and the `call_id` result file exists, jump to `pytest`.
2. If `resume_from == "invoke-subagent"` and no result file exists:
   - if the working tree is clean inside `owned_paths`, retry the same prompt
   - if the working tree is dirty inside `owned_paths`, mark task `blocked` and require Claude Code review
3. If `resume_from == "pytest"`, rerun targeted pytest from the stored attempt bundle.
4. If `resume_from == "cc-review"`, do not touch files; Claude Code reads the attempt bundle and writes a review decision.
5. If `resume_from == "commit"`, restage only the approved task files and create the commit.

This keeps resume conservative around partial edits instead of guessing.

## Controller Responsibilities

The controller is a resumable state machine, not an unattended autonomous builder.

Deterministic responsibilities:

- choose the next ready task
- render the Codex prompt
- invoke `cross-agent.py`
- capture the raw result and changed files
- run targeted pytest
- gather the diff and line counts
- write a review packet
- stage and commit approved files
- log everything

Claude Code responsibilities:

- author the manifest
- review the diff against acceptance criteria
- decide `approve` vs `correct`
- write the correction prompt when needed
- decide whether a failed task should be retried, manually fixed, or re-scoped

That split is intentional. It is the smallest design that is both useful and honest.

## Core Loop

### Ready Task Selection

A task is ready when:

- its state status is `pending` or `correcting`
- every dependency is `done`
- its `attempt_count < max_attempts_per_task`

Tie-breaker:

1. lowest phase-local task id in natural sort order
2. if equal, fewer prior attempts

### Preflight Checks

Before invoking Codex for a task:

1. `git status --porcelain`
2. fail if files overlapping `owned_paths` already have unrelated modifications
3. fail if task would exceed 10 files of expected change scope
4. ensure repo-root `state/cross-agent/` exists or can be created

### Exact Cross-Agent Invocation

The controller calls the shared runner directly from Python using an argument list, not a shell string:

```text
python C:/Users/ColsonR/Agent/runner/cross-agent.py
  --direction cc-to-codex
  --task-type implement
  --prompt "<rendered prompt text>"
  --working-dir C:/Users/ColsonR/kraken-bot-v4
  --owned-paths core/config.py core/errors.py tests/core/test_config.py
  --timeout 600
```

Notes:

- Use `subprocess.run([...])`, not `shell=True`.
- Prompt text is passed directly as the `--prompt` value.
- Because Windows command lines have finite length, prompts must be compact. The prompt should reference `SPEC.md` sections by path and heading instead of embedding large excerpts.
- The controller should save the full rendered prompt to `attempt-N/implement.prompt.md` even though the runner only accepts the text string.

### Pseudocode

```python
def run_phase(manifest_path: str) -> int:
    manifest = load_manifest(manifest_path)
    state = load_or_init_phase_state(manifest)

    while True:
        task = next_ready_task(manifest, state)
        if task is None:
            return finalize_phase_if_complete(manifest, state)

        ensure_clean_overlap(task["owned_paths"])
        attempt = start_attempt(state, task)
        artifacts = make_attempt_dir(task["id"], attempt["attempt"])

        prompt_text = render_task_prompt(manifest, task, state, mode=attempt["mode"])
        write_text(artifacts / "implement.prompt.md", prompt_text)
        update_task_resume(state, task["id"], "invoke-subagent", artifacts)

        result = invoke_codex(prompt_text, task["owned_paths"])
        write_json(artifacts / "codex.stdout.json", result)
        copy_cross_agent_files(result["call_id"], artifacts)

        actual_changed = git_changed_files(task["owned_paths"])
        write_text(artifacts / "diff.patch", git_diff(task["owned_paths"]))
        record_changed_files(state, task["id"], actual_changed)

        update_task_status(state, task["id"], "verifying", "pytest")
        pytest_exit, pytest_out = run_targeted_pytest(task["test_targets"])
        write_text(artifacts / "pytest.txt", pytest_out)
        record_pytest_result(state, task["id"], pytest_exit)

        review_packet = build_review_packet(
            manifest=manifest,
            task=task,
            state=state,
            changed_files=actual_changed,
            pytest_output=pytest_out,
            diff_text=read_text(artifacts / "diff.patch"),
            line_counts=line_counts(actual_changed),
        )
        write_text(artifacts / "review.md", review_packet)
        update_task_resume(state, task["id"], "cc-review", artifacts)

        decision = read_or_request_cc_decision(task["id"], artifacts)
        # decision is one of: approve | correct | fail

        if decision.kind == "approve" and pytest_exit == 0:
            update_task_resume(state, task["id"], "commit", artifacts)
            commit_sha = commit_task(task, actual_changed, artifacts)
            mark_task_done(state, task["id"], commit_sha)
            log_event("task_done", task_id=task["id"], commit_sha=commit_sha)
            continue

        if attempt["attempt"] >= manifest["global_constraints"]["max_attempts_per_task"]:
            mark_task_failed(
                state,
                task["id"],
                reason="attempt limit exhausted after review/test failures"
            )
            log_event("task_failed", task_id=task["id"])
            return 1

        correction_text = render_correction_prompt(
            manifest=manifest,
            task=task,
            pytest_output=pytest_out,
            review_decision=decision,
            changed_files=actual_changed,
        )
        write_text(artifacts / "correction.prompt.md", correction_text)
        mark_task_correcting(state, task["id"], decision.reason)
        continue
```

### Review Boundary

`read_or_request_cc_decision()` is the intentional human/Claude boundary.

V1 behavior:

- The controller writes `review.md` with:
  - acceptance criteria
  - changed files
  - targeted pytest output
  - file line counts
  - warnings if any changed file exceeds 500 lines
  - warnings if Codex touched files outside `owned_paths`
- Claude Code reads that packet and writes `review.decision.json`:

```json
{
  "decision": "approve",
  "reason": "Acceptance criteria met and targeted pytest passed."
}
```

or:

```json
{
  "decision": "correct",
  "reason": "OrderGate uses a broad exception and missed cl_ord_id propagation.",
  "must_fix": [
    "replace broad exception with typed Kraken error",
    "thread cl_ord_id through AddOrder payload",
    "keep file under 500 lines"
  ]
}
```

This is the only part of the loop that is not purely deterministic, and that is acceptable.

## Correction Strategy

Correction is a new attempt on the same task id.

Rules:

- `attempt 1` = initial implementation
- `attempt 2` = first correction
- `attempt 3` = final correction
- after `attempt 3` fails review or tests, task status becomes `failed` and the phase becomes `blocked`

The correction prompt must be narrower than the original prompt. It should tell Codex what to preserve and what to change.

## Git Commit Policy

### What Gets Staged

Stage only:

- files actually changed by the approved task
- matching test files

Never stage:

- `build/state/**`
- `build/logs/**`
- `state/cross-agent/**`
- unrelated dirty files outside `owned_paths`

### Commit Message Format

Subject:

```text
build(phase-<phase-id>/<task-id>): <imperative summary>
```

Body:

```text
Phase: <phase name>
Task: <task id>
Manifest: build/manifests/<phase file>.json
Attempt: <n>/<max>
Tests: python -m pytest -q <targets>
Call-ID: <cross-agent call id>
Verified-By: Claude Code diff review + targeted pytest
```

Example:

```text
build(phase-1/1.5): add OrderGate cl_ord_id and circuit breaker

Phase: Foundation
Task: 1.5
Manifest: build/manifests/phase-1.foundation.json
Attempt: 2/3
Tests: python -m pytest -q tests/exchange/test_order_gate.py
Call-ID: a1b2c3d4e5f6
Verified-By: Claude Code diff review + targeted pytest
```

## CC/Codex Handoff Protocol

## What Claude Code Sends

Each implementation prompt contains:

1. task id and phase id
2. task description
3. exact acceptance criteria
4. exact `owned_paths`
5. exact `pytest` targets Claude Code will run afterward
6. relevant `SPEC.md` references by section heading
7. V4 constraints:
   - Python only
   - typed exceptions only
   - prefer frozen dataclasses
   - keep files under 500 lines
   - no git commits
   - do not run tests

## What Codex Returns

The controller expects the cross-agent result schema already enforced by the shared runner:

- `call_id`
- `status`
- `result.summary`
- `result.files_changed`
- `result.notes`

The controller treats `result.files_changed` as advisory and computes the authoritative changed file set from git diff after the run.

## What Claude Code Sends on Correction

The correction prompt contains:

1. the original task description
2. what Codex already changed that must remain
3. failed `pytest` output
4. specific review findings
5. exact fixes required
6. the same `owned_paths`
7. the same no-test / no-commit constraints

## V4-Specific Preamble Additions

The shared subagent preambles remain in the `Agent` repo. `build/` adds project-specific context inside the task prompt itself:

- repo root is `kraken-bot-v4`
- follow [SPEC.md](/C:/Users/ColsonR/kraken-bot-v4/SPEC.md)
- obey 500-line module cap
- prefer frozen dataclasses
- typed exceptions only
- no broad `except Exception`
- state machine stays pure
- tests go under `tests/`

## Example Manifest: Phase 1

```json
{
  "schema_version": "build-manifest/v1",
  "project": "kraken-bot-v4",
  "phase": {
    "id": "1",
    "name": "Foundation",
    "spec_ref": "SPEC.md#Implementation Phases > Phase 1: Foundation",
    "created_at": "2026-03-23T22:00:00-04:00",
    "created_by": "claude-code"
  },
  "global_constraints": {
    "max_attempts_per_task": 3,
    "max_files_changed_per_task": 10,
    "module_line_cap": 500,
    "typed_exceptions_only": true,
    "frozen_dataclasses_preferred": true,
    "no_git_push": true
  },
  "tasks": [
    {
      "id": "1.1",
      "description": "Create env-driven config scaffolding and typed error base classes.",
      "acceptance_criteria": [
        "Settings load from environment with explicit defaults.",
        "Typed error hierarchy exists for config and exchange failures.",
        "Tests cover missing required env and default loading."
      ],
      "owned_paths": [
        "core/config.py",
        "core/errors.py",
        "tests/core/test_config.py"
      ],
      "dependencies": [],
      "status": "pending",
      "test_targets": [
        "tests/core/test_config.py"
      ]
    },
    {
      "id": "1.2",
      "description": "Create frozen dataclasses for core state and a pure reducer skeleton.",
      "acceptance_criteria": [
        "Core state models are frozen dataclasses.",
        "Reducer has no I/O and returns (state, actions).",
        "Reducer tests cover an initial no-op transition."
      ],
      "owned_paths": [
        "core/types.py",
        "core/state_machine.py",
        "tests/core/test_state_machine.py"
      ],
      "dependencies": ["1.1"],
      "status": "pending",
      "test_targets": [
        "tests/core/test_state_machine.py"
      ]
    },
    {
      "id": "1.3",
      "description": "Implement AP Stats normality gate utilities and initial validation tests.",
      "acceptance_criteria": [
        "Normality gate exists as a reusable function.",
        "Outputs are deterministic for known sample datasets.",
        "Tests cover pass and fail cases."
      ],
      "owned_paths": [
        "stats/normality.py",
        "tests/stats/test_normality.py"
      ],
      "dependencies": ["1.1"],
      "status": "pending",
      "test_targets": [
        "tests/stats/test_normality.py"
      ]
    },
    {
      "id": "1.4",
      "description": "Create symbol normalization and Kraken Starter-tier rate limiter scaffolding.",
      "acceptance_criteria": [
        "A canonical symbol normalizer exists for Kraken pairs.",
        "Starter-tier rate limiter rules are encoded inside exchange/client.py.",
        "Tests cover representative symbol and limiter cases."
      ],
      "owned_paths": [
        "exchange/symbols.py",
        "exchange/client.py",
        "tests/exchange/test_symbols.py",
        "tests/exchange/test_client.py"
      ],
      "dependencies": ["1.1"],
      "status": "pending",
      "test_targets": [
        "tests/exchange/test_symbols.py",
        "tests/exchange/test_client.py"
      ]
    },
    {
      "id": "1.5",
      "description": "Implement OrderGate scaffolding with cl_ord_id generation and a circuit breaker shell.",
      "acceptance_criteria": [
        "OrderGate is the only module that constructs order payloads.",
        "Every emitted order payload includes cl_ord_id.",
        "Circuit breaker shell blocks mutations after repeated failures.",
        "Tests cover cl_ord_id generation and breaker trip behavior."
      ],
      "owned_paths": [
        "exchange/order_gate.py",
        "tests/exchange/test_order_gate.py"
      ],
      "dependencies": ["1.2", "1.4"],
      "status": "pending",
      "test_targets": [
        "tests/exchange/test_order_gate.py"
      ]
    },
    {
      "id": "1.6",
      "description": "Create persistence scaffolding for Supabase client with integrated offline queue.",
      "acceptance_criteria": [
        "Supabase client interface exists without embedding secrets.",
        "Offline queue is integrated inside persistence/supabase.py.",
        "Tests cover queue enqueue/dequeue behavior."
      ],
      "owned_paths": [
        "persistence/supabase.py",
        "tests/persistence/test_supabase.py"
      ],
      "dependencies": ["1.1"],
      "status": "pending",
      "test_targets": [
        "tests/persistence/test_supabase.py"
      ]
    }
  ]
}
```

## Example CC → Codex Prompt

Example for task `1.5`:

```md
Task ID: 1.5
Phase: 1 Foundation
Repo: C:/Users/ColsonR/kraken-bot-v4

Read before editing:
- SPEC.md -> "Order Gate"
- SPEC.md -> "Phase 1: Foundation"
- build/manifests/phase-1.foundation.json -> task 1.5

Implement this task:
Create `exchange/order_gate.py` and `tests/exchange/test_order_gate.py`.

Acceptance criteria:
- OrderGate is the only module that constructs order payloads.
- Every emitted order payload includes a deterministic `cl_ord_id`.
- Circuit breaker shell blocks new order mutations after repeated failures.
- Tests cover `cl_ord_id` generation and breaker trip behavior.

Owned paths:
- exchange/order_gate.py
- tests/exchange/test_order_gate.py

Constraints:
- Python only.
- Keep each file under 500 lines.
- Use typed exceptions only. No broad `except Exception`.
- Do not run tests, lint, or git commands.
- Do not modify files outside owned paths.
- Keep implementation small and scaffold-friendly; do not build the full live Kraken client here.

Claude Code will verify with:
- python -m pytest -q tests/exchange/test_order_gate.py

Return a normal cross-agent result JSON and exit.
```

## Example Correction Prompt

Example after a failing test and review rejection:

```md
Task ID: 1.5
Phase: 1 Foundation
Attempt: 2 of 3

Keep these existing changes:
- `exchange/order_gate.py` already contains the OrderGate class skeleton.
- Existing test names should remain unchanged where possible.

Why the prior attempt was rejected:
- `python -m pytest -q tests/exchange/test_order_gate.py` failed.
- `test_order_payload_includes_cl_ord_id` shows the payload builder omitted `cl_ord_id`.
- Review also found a broad exception around breaker updates.

Required fixes:
1. Thread `cl_ord_id` into every emitted order payload.
2. Replace the broad exception with a typed error path.
3. Keep the file under 500 lines.
4. Do not widen scope beyond the current owned paths.

Owned paths:
- exchange/order_gate.py
- tests/exchange/test_order_gate.py

Do not run tests or git commands. Claude Code will re-run the same pytest target after this pass.
```

## Phase Boundary Actions

When all tasks in a phase are `done`:

1. Run full test suite:

   ```text
   python -m pytest -q
   ```

2. Run lint:

   ```text
   ruff check .
   ```

3. Write `build/state/phase-N.slug.summary.md` containing:
   - phase id and name
   - task list with attempts used
   - commit SHAs
   - full pytest status
   - lint status
   - blockers carried forward

4. Mark phase:
   - `done` if full pytest and lint pass
   - `blocked` otherwise

5. Never push automatically. Pushing remains a manual Claude Code or user action.

## Recommended Implementation Order

1. Add `build/` tracked structure and ignore rules.
2. Implement stdlib-only manifest/state validation.
3. Implement `run_phase.py` with one-task-at-a-time execution.
4. Add review packet generation and resume logic.
5. Add phase-boundary full-suite and lint actions.

That sequence keeps the first usable version small and dogfoodable.
