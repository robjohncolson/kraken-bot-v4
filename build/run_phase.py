from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import (
    BUILD_STATE_DIR,
    CROSS_AGENT_STATE_DIR,
    REPO_ROOT,
    BuildError,
    CommandError,
    ValidationError,
    atomic_write_json,
    atomic_write_text,
    build_review_packet,
    build_review_prompt,
    changed_paths_for_owned_paths,
    commit_message,
    copy_cross_agent_artifacts,
    current_python,
    ensure_dir,
    git_commit,
    git_diff,
    git_stage,
    invoke_codex,
    invoke_codex_review,
    line_count_for_repo_file,
    load_manifest,
    load_or_init_phase_state,
    load_review_decision,
    log_event,
    natural_task_key,
    now_iso,
    normalize_cross_agent_result,
    parse_codex_review_verdict,
    phase_summary_path,
    prompt_for_task,
    read_json,
    rel_repo_path,
    run_command,
    run_targeted_pytest,
    save_phase_state,
    task_attempt_dir,
    which_or_none,
    write_review_example,
    review_decision_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one build phase manifest.")
    parser.add_argument("manifest", help="Path to a JSON phase manifest.")
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Cross-agent subtask timeout in seconds (default: 600).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the next action instead of invoking Codex or running tests.",
    )
    parser.add_argument(
        "--manual-review",
        action="store_true",
        help="Stop at review boundary instead of auto-reviewing via Codex.",
    )
    return parser.parse_args()


def manifest_tasks(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {task["id"]: task for task in manifest["tasks"]}


def task_state(state: dict[str, Any], task_id: str) -> dict[str, Any]:
    return state["tasks"][task_id]


def deps_done(manifest_map: dict[str, dict[str, Any]], state: dict[str, Any], task_id: str) -> bool:
    for dep in manifest_map[task_id]["dependencies"]:
        if task_state(state, dep)["status"] != "done":
            return False
    return True


def current_task_id(state: dict[str, Any]) -> str | None:
    task_id = state.get("current_task_id")
    if task_id and task_state(state, task_id)["status"] not in {"done", "failed", "blocked"}:
        return task_id
    return None


def next_ready_task(manifest: dict[str, Any], state: dict[str, Any]) -> str | None:
    manifest_map = manifest_tasks(manifest)
    current_id = current_task_id(state)
    if current_id:
        return current_id

    candidates: list[str] = []
    max_attempts = manifest["global_constraints"]["max_attempts_per_task"]
    for task_id in sorted(manifest_map, key=natural_task_key):
        entry = task_state(state, task_id)
        if entry["status"] not in {"pending", "correcting"}:
            continue
        if entry["attempt_count"] >= max_attempts:
            continue
        if deps_done(manifest_map, state, task_id):
            candidates.append(task_id)
    return candidates[0] if candidates else None


def ensure_clean_overlap(task: dict[str, Any]) -> None:
    dirty = changed_paths_for_owned_paths(task["owned_paths"])
    if dirty:
        raise BuildError(
            f"Owned paths already dirty for task {task['id']}: {', '.join(dirty)}. "
            "Resolve or commit them before starting a fresh task."
        )


def start_attempt(
    manifest_path: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    task_id: str,
    mode: str,
) -> tuple[dict[str, Any], Path]:
    entry = task_state(state, task_id)
    attempt_no = entry["attempt_count"] + 1
    artifacts_dir = task_attempt_dir(task_id, attempt_no)
    ensure_dir(artifacts_dir)
    attempt_record = {
        "attempt": attempt_no,
        "mode": mode,
        "started_at": now_iso(),
        "finished_at": None,
        "call_id": None,
        "artifacts_dir": rel_repo_path(artifacts_dir),
        "changed_files": [],
        "pytest_exit_code": None,
        "review_decision": None,
        "review_notes_path": None,
    }
    entry["attempt_count"] = attempt_no
    entry["status"] = "in-progress"
    entry["resume_from"] = "invoke-subagent"
    entry["last_error"] = None
    entry["attempts"].append(attempt_record)
    state["phase_status"] = "active"
    state["current_task_id"] = task_id
    save_phase_state(manifest_path, state)
    log_event("task_attempt_started", phase=manifest["phase"]["id"], task_id=task_id, attempt=attempt_no, mode=mode)
    return attempt_record, artifacts_dir


def latest_attempt(entry: dict[str, Any]) -> dict[str, Any]:
    if not entry["attempts"]:
        raise BuildError("Task has no attempts recorded.")
    return entry["attempts"][-1]


def auto_review_decision(
    manifest: dict[str, Any],
    task: dict[str, Any],
    pytest_exit: int,
    changed_files: list[str],
    codex_verdict: str,
    codex_issues: list[str],
) -> dict[str, Any]:
    """Produce a review decision from deterministic checks + Codex review."""
    issues: list[str] = []
    cap = manifest["global_constraints"]["module_line_cap"]

    # Check 1: pytest must pass
    if pytest_exit != 0:
        issues.append(f"Targeted pytest failed with exit code {pytest_exit}.")

    # Check 2: line cap
    for path in changed_files:
        count = line_count_for_repo_file(path)
        if count is not None and count > cap:
            issues.append(f"{path} is {count} lines, exceeding the {cap}-line cap.")

    # Check 3: scope — files outside owned_paths
    owned = set(task["owned_paths"])
    for path in changed_files:
        if path not in owned:
            issues.append(f"{path} was changed but is not in owned_paths.")

    # Check 4: Codex review findings
    if codex_verdict == "correct" and codex_issues:
        issues.extend(codex_issues)

    if issues:
        return {
            "decision": "correct",
            "reason": "; ".join(issues[:3]),
            "must_fix": issues,
        }
    return {
        "decision": "approve",
        "reason": "Pytest passed, all files under line cap, scope clean, Codex review approved.",
    }


def finish_verification(
    manifest_path: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    task: dict[str, Any],
    attempt: dict[str, Any],
    attempt_dir: Path,
    runner_result: dict[str, Any],
    changed: list[str],
    manual_review: bool = False,
) -> int:
    entry = task_state(state, task["id"])
    entry["status"] = "verifying"
    entry["resume_from"] = "pytest"
    save_phase_state(manifest_path, state)

    pytest_exit, pytest_output = run_targeted_pytest(task["test_targets"])
    attempt["pytest_exit_code"] = pytest_exit
    atomic_write_text(attempt_dir / "pytest.txt", pytest_output)

    review_text = build_review_packet(manifest, task, runner_result, changed, pytest_exit, pytest_output)
    atomic_write_text(attempt_dir / "review.md", review_text)

    if manual_review:
        entry["status"] = "verifying"
        entry["resume_from"] = "cc-review"
        state["phase_status"] = "awaiting-review"
        save_phase_state(manifest_path, state)
        write_review_example(review_decision_path(attempt_dir))
        log_event("task_awaiting_review", phase=manifest["phase"]["id"], task_id=task["id"], attempt=attempt["attempt"])
        print(f"Review required for task {task['id']}: {attempt_dir / 'review.md'}")
        return 2

    # Auto-review: invoke Codex as independent reviewer
    print(f"Auto-reviewing task {task['id']} via Codex...")
    review_prompt = build_review_prompt(manifest, task, changed, pytest_exit, pytest_output)
    codex_review = invoke_codex_review(review_prompt)
    atomic_write_json(attempt_dir / "codex-review.json", codex_review)
    codex_verdict, codex_issues = parse_codex_review_verdict(codex_review)

    decision = auto_review_decision(manifest, task, pytest_exit, changed, codex_verdict, codex_issues)
    atomic_write_json(review_decision_path(attempt_dir), decision)
    log_event(
        "auto_review",
        phase=manifest["phase"]["id"],
        task_id=task["id"],
        attempt=attempt["attempt"],
        decision=decision["decision"],
        codex_verdict=codex_verdict,
    )
    print(f"Auto-review for {task['id']}: {decision['decision']} - {decision['reason'][:100]}")

    entry["resume_from"] = "cc-review"
    save_phase_state(manifest_path, state)
    return process_review_decision(manifest_path, manifest, state, task)


def resume_incomplete_attempt(
    manifest_path: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    task: dict[str, Any],
    timeout: int,
    manual_review: bool = False,
) -> int:
    entry = task_state(state, task["id"])
    attempt = latest_attempt(entry)
    attempt_dir = REPO_ROOT / attempt["artifacts_dir"]
    if entry["resume_from"] == "pytest":
        result_path = attempt_dir / "cross-agent.result.json"
        if not result_path.exists():
            raise BuildError(f"Missing cross-agent result for task {task['id']} at {result_path}")
        runner_result = normalize_cross_agent_result(read_json(result_path), fallback_call_id=attempt.get("call_id"))
        changed = changed_paths_for_owned_paths(task["owned_paths"])
        attempt["changed_files"] = changed
        atomic_write_text(attempt_dir / "diff.patch", git_diff(task["owned_paths"]))
        return finish_verification(manifest_path, manifest, state, task, attempt, attempt_dir, runner_result, changed, manual_review)

    if entry["resume_from"] != "invoke-subagent":
        raise BuildError(f"Cannot resume task {task['id']} from {entry['resume_from']}")

    if attempt.get("call_id"):
        result_file = CROSS_AGENT_STATE_DIR / f"{attempt['call_id']}.result.json"
        if result_file.exists():
            copy_cross_agent_artifacts(attempt["call_id"], attempt_dir)
            runner_result = normalize_cross_agent_result(read_json(result_file), fallback_call_id=attempt["call_id"])
            changed = changed_paths_for_owned_paths(task["owned_paths"])
            attempt["changed_files"] = changed
            atomic_write_text(attempt_dir / "diff.patch", git_diff(task["owned_paths"]))
            return finish_verification(manifest_path, manifest, state, task, attempt, attempt_dir, runner_result, changed, manual_review)

    dirty = changed_paths_for_owned_paths(task["owned_paths"])
    if dirty:
        entry["status"] = "blocked"
        entry["resume_from"] = "complete"
        entry["last_error"] = (
            "Task interrupted during subagent invocation and owned paths are dirty. "
            "Manual review required before retry."
        )
        state["phase_status"] = "blocked"
        save_phase_state(manifest_path, state)
        log_event("task_blocked", phase=manifest["phase"]["id"], task_id=task["id"], reason=entry["last_error"])
        return 1

    prompt_name = "correction.prompt.md" if attempt["mode"] == "correct" else "implement.prompt.md"
    prompt_path = attempt_dir / prompt_name
    if not prompt_path.exists():
        raise BuildError(f"Missing saved prompt for task {task['id']}: {prompt_path}")
    prompt_text = prompt_path.read_text(encoding="utf-8")
    runner_result, stderr_text = invoke_codex(prompt_text, task["owned_paths"], timeout, dry_run=False)
    atomic_write_json(attempt_dir / "codex.stdout.json", runner_result)
    atomic_write_text(attempt_dir / "codex.stderr.txt", stderr_text)
    attempt["call_id"] = runner_result.get("call_id")
    copy_cross_agent_artifacts(attempt["call_id"], attempt_dir)
    if runner_result.get("status") != "completed":
        entry["status"] = "failed"
        entry["resume_from"] = "complete"
        entry["last_error"] = runner_result.get("result", {}).get("notes", "subagent failed on resume")
        state["phase_status"] = "blocked"
        save_phase_state(manifest_path, state)
        return 1
    changed = changed_paths_for_owned_paths(task["owned_paths"])
    attempt["changed_files"] = changed
    atomic_write_text(attempt_dir / "diff.patch", git_diff(task["owned_paths"]))
    return finish_verification(manifest_path, manifest, state, task, attempt, attempt_dir, runner_result, changed, manual_review)


def process_review_decision(
    manifest_path: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    task: dict[str, Any],
) -> int:
    entry = task_state(state, task["id"])
    attempt = latest_attempt(entry)
    attempt_dir = REPO_ROOT / attempt["artifacts_dir"]
    decision_path = review_decision_path(attempt_dir)
    decision = load_review_decision(decision_path)
    if decision is None:
        write_review_example(decision_path)
        state["phase_status"] = "awaiting-review"
        save_phase_state(manifest_path, state)
        print(f"Awaiting review decision: {decision_path}")
        return 2

    attempt["review_decision"] = decision["decision"]
    attempt["review_notes_path"] = rel_repo_path(decision_path)
    atomic_write_json(attempt_dir / "review.decision.json", decision)
    pytest_exit = attempt.get("pytest_exit_code")
    if decision["decision"] == "approve":
        if pytest_exit not in (0, None):
            raise BuildError(f"Task {task['id']} cannot be approved while targeted pytest is failing.")
        current_changed = changed_paths_for_owned_paths(task["owned_paths"])
        if not current_changed:
            raise BuildError(f"Task {task['id']} has no changed files to commit.")
        git_stage(current_changed)
        message = commit_message(manifest_path, manifest, task, attempt)
        commit_path = attempt_dir / "commit.txt"
        commit_sha = git_commit(message, current_changed, commit_path)
        entry["status"] = "done"
        entry["resume_from"] = "complete"
        entry["commit_sha"] = commit_sha
        attempt["finished_at"] = now_iso()
        state["current_task_id"] = None
        state["phase_status"] = "active"
        save_phase_state(manifest_path, state)
        log_event("task_done", phase=manifest["phase"]["id"], task_id=task["id"], commit_sha=commit_sha)
        print(f"Committed task {task['id']} as {commit_sha}")
        return 0

    if decision["decision"] == "fail":
        entry["status"] = "failed"
        entry["resume_from"] = "complete"
        entry["last_error"] = decision["reason"]
        attempt["finished_at"] = now_iso()
        state["current_task_id"] = None
        state["phase_status"] = "blocked"
        save_phase_state(manifest_path, state)
        log_event("task_failed", phase=manifest["phase"]["id"], task_id=task["id"], reason=decision["reason"])
        return 1

    if entry["attempt_count"] >= manifest["global_constraints"]["max_attempts_per_task"]:
        entry["status"] = "failed"
        entry["resume_from"] = "complete"
        entry["last_error"] = "Attempt limit exhausted."
        attempt["finished_at"] = now_iso()
        state["current_task_id"] = None
        state["phase_status"] = "blocked"
        save_phase_state(manifest_path, state)
        log_event("task_failed", phase=manifest["phase"]["id"], task_id=task["id"], reason="attempt limit exhausted")
        return 1

    entry["status"] = "correcting"
    entry["resume_from"] = "start"
    entry["last_error"] = decision["reason"]
    save_phase_state(manifest_path, state)
    log_event("task_correcting", phase=manifest["phase"]["id"], task_id=task["id"], reason=decision["reason"])
    return 0


def finalize_phase(manifest_path: Path, manifest: dict[str, Any], state: dict[str, Any]) -> int:
    if any(entry["status"] in {"failed", "blocked"} for entry in state["tasks"].values()):
        state["phase_status"] = "blocked"
        save_phase_state(manifest_path, state)
        return 1
    if any(entry["status"] != "done" for entry in state["tasks"].values()):
        return 0

    full_pytest = run_command([current_python(), "-m", "pytest", "-q"], cwd=REPO_ROOT, check=False)
    ruff_bin = which_or_none("ruff")
    ruff_cmd = [ruff_bin, "check", "."] if ruff_bin else [current_python(), "-m", "ruff", "check", "."]
    ruff = run_command(ruff_cmd, cwd=REPO_ROOT, check=False)
    ruff_text = (ruff.stdout + ("\n" if ruff.stdout and ruff.stderr else "") + ruff.stderr).strip()
    ruff_code = ruff.returncode

    summary_lines = [
        f"# Phase Summary: {manifest['phase']['id']} {manifest['phase']['name']}",
        "",
        f"Manifest: {rel_repo_path(manifest_path)}",
        "",
        "Tasks:",
    ]
    for task_id in sorted(state["tasks"], key=natural_task_key):
        entry = state["tasks"][task_id]
        summary_lines.append(f"- {task_id}: {entry['status']} (attempts: {entry['attempt_count']}, commit: {entry['commit_sha']})")
    summary_lines.extend(
        [
            "",
            "Full pytest:",
            "```text",
            (full_pytest.stdout + ("\n" if full_pytest.stdout and full_pytest.stderr else "") + full_pytest.stderr).strip(),
            "```",
            "",
            "Ruff:",
            "```text",
            ruff_text,
            "```",
            "",
            "Push remains manual.",
        ]
    )
    atomic_write_text(phase_summary_path(manifest_path), "\n".join(summary_lines).strip() + "\n")

    if full_pytest.returncode == 0 and ruff_code == 0:
        state["phase_status"] = "done"
        save_phase_state(manifest_path, state)
        log_event("phase_done", phase=manifest["phase"]["id"])
        print("Phase complete.")
        return 0

    state["phase_status"] = "blocked"
    save_phase_state(manifest_path, state)
    log_event("phase_blocked", phase=manifest["phase"]["id"], reason="full-suite or lint failed")
    print("Phase completed task-wise but blocked by full-suite or lint failures.")
    return 1


def run_task_attempt(
    manifest_path: Path,
    manifest: dict[str, Any],
    state: dict[str, Any],
    task: dict[str, Any],
    timeout: int,
    dry_run: bool,
    manual_review: bool = False,
) -> int:
    entry = task_state(state, task["id"])
    if entry["status"] == "pending":
        ensure_clean_overlap(task)
        mode = "implement"
        decision = None
    elif entry["status"] == "correcting":
        mode = "correct"
        prior_attempt = latest_attempt(entry)
        prior_dir = REPO_ROOT / prior_attempt["artifacts_dir"]
        decision = load_review_decision(review_decision_path(prior_dir))
        if decision is None:
            raise BuildError(f"Missing review decision for correcting task {task['id']}")
    else:
        raise BuildError(f"Task {task['id']} is not ready for a new attempt from status {entry['status']}")

    prompt_text = prompt_for_task(manifest_path, manifest, task, mode, decision)

    if dry_run:
        print(f"Dry run: would invoke Codex for task {task['id']} ({mode})")
        print(prompt_text)
        return 3

    attempt, attempt_dir = start_attempt(manifest_path, manifest, state, task["id"], mode)
    prompt_name = "correction.prompt.md" if mode == "correct" else "implement.prompt.md"
    atomic_write_text(attempt_dir / prompt_name, prompt_text)

    runner_result, stderr_text = invoke_codex(prompt_text, task["owned_paths"], timeout, dry_run=False)
    atomic_write_json(attempt_dir / "codex.stdout.json", runner_result)
    atomic_write_text(attempt_dir / "codex.stderr.txt", stderr_text)

    attempt["call_id"] = runner_result.get("call_id")
    copy_cross_agent_artifacts(attempt["call_id"], attempt_dir)
    if runner_result.get("status") != "completed":
        entry["status"] = "failed"
        entry["resume_from"] = "complete"
        entry["last_error"] = runner_result.get("result", {}).get("notes", "subagent failed")
        attempt["finished_at"] = now_iso()
        state["phase_status"] = "blocked"
        save_phase_state(manifest_path, state)
        log_event("task_failed", phase=manifest["phase"]["id"], task_id=task["id"], reason=entry["last_error"])
        return 1

    changed = changed_paths_for_owned_paths(task["owned_paths"])
    if len(changed) > manifest["global_constraints"]["max_files_changed_per_task"]:
        raise BuildError(
            f"Task {task['id']} changed {len(changed)} files, exceeding max_files_changed_per_task="
            f"{manifest['global_constraints']['max_files_changed_per_task']}"
        )
    attempt["changed_files"] = changed
    atomic_write_text(attempt_dir / "diff.patch", git_diff(task["owned_paths"]))
    return finish_verification(manifest_path, manifest, state, task, attempt, attempt_dir, runner_result, changed, manual_review)


def controller(manifest_path: Path, timeout: int, dry_run: bool, manual_review: bool = False) -> int:
    manifest = load_manifest(manifest_path)
    state = load_or_init_phase_state(manifest, manifest_path)
    while True:
        task_id = next_ready_task(manifest, state)
        if task_id is None:
            return finalize_phase(manifest_path, manifest, state)

        task = manifest_tasks(manifest)[task_id]
        entry = task_state(state, task_id)

        if entry["resume_from"] == "cc-review":
            result = process_review_decision(manifest_path, manifest, state, task)
            if result != 0:
                return result
            continue

        if entry["resume_from"] in {"invoke-subagent", "pytest"}:
            result = resume_incomplete_attempt(manifest_path, manifest, state, task, timeout, manual_review)
            if result != 0:
                return result
            continue

        if entry["status"] in {"pending", "correcting"}:
            result = run_task_attempt(manifest_path, manifest, state, task, timeout, dry_run, manual_review)
            if result == 3:
                return 0
            if result != 0:
                return result
            continue

        if entry["status"] == "verifying":
            attempt_dir = REPO_ROOT / latest_attempt(entry)["artifacts_dir"]
            print(f"Task {task_id} is awaiting review: {attempt_dir / 'review.md'}")
            return 2

        if entry["status"] in {"failed", "blocked"}:
            state["phase_status"] = "blocked"
            save_phase_state(manifest_path, state)
            return 1

        if entry["status"] == "done":
            state["current_task_id"] = None
            save_phase_state(manifest_path, state)
            continue

        raise BuildError(f"Unhandled task state for {task_id}: {entry['status']}")


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest)
    if not manifest_path.is_absolute():
        manifest_path = (REPO_ROOT / manifest_path).resolve()
    ensure_dir(BUILD_STATE_DIR)
    ensure_dir(CROSS_AGENT_STATE_DIR)
    try:
        return controller(manifest_path, args.timeout, args.dry_run, args.manual_review)
    except (BuildError, CommandError, ValidationError) as exc:
        print(f"ERROR: {exc}")
        log_event("controller_error", manifest=rel_repo_path(manifest_path), error=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
