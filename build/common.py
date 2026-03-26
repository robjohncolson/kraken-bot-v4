from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_DIR = REPO_ROOT / "build"
BUILD_STATE_DIR = BUILD_DIR / "state"
BUILD_LOGS_DIR = BUILD_DIR / "logs"
CROSS_AGENT_STATE_DIR = REPO_ROOT / "state" / "cross-agent"
def _resolve_agent_runner() -> Path:
    """Resolve cross-agent runner path from env or well-known locations."""
    env_path = os.environ.get("CROSS_AGENT_RUNNER")
    if env_path:
        return Path(env_path)
    candidates = [
        Path(r"C:\Users\rober\Downloads\Projects\Agent\runner\cross-agent.py"),
        Path(r"C:\Users\ColsonR\Agent\runner\cross-agent.py"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


AGENT_RUNNER = _resolve_agent_runner()

MANIFEST_VERSION = "build-manifest/v1"
STATE_VERSION = "build-state/v1"
TASK_STATUSES = {
    "pending",
    "in-progress",
    "verifying",
    "correcting",
    "done",
    "failed",
    "blocked",
}
PHASE_STATUSES = {"pending", "active", "awaiting-review", "done", "failed", "blocked"}
REVIEW_DECISIONS = {"approve", "correct", "fail"}


class BuildError(RuntimeError):
    """Base error for build orchestration failures."""


class ValidationError(BuildError):
    """Raised when manifest or state files are structurally invalid."""


class CommandError(BuildError):
    """Raised when a subprocess exits unsuccessfully."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rel_repo_path(path: Path | str) -> str:
    path_obj = Path(path)
    if not path_obj.is_absolute():
        return path_obj.as_posix()
    return path_obj.relative_to(REPO_ROOT).as_posix()


def build_log_path() -> Path:
    ensure_dir(BUILD_LOGS_DIR)
    return BUILD_LOGS_DIR / "build-loop.ndjson"


def log_event(event: str, **fields: Any) -> None:
    payload = {"ts": now_iso(), "event": event, **fields}
    ensure_dir(BUILD_LOGS_DIR)
    with (BUILD_LOGS_DIR / "build-loop.ndjson").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def natural_task_key(task_id: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in task_id.split("."))
    except ValueError as exc:
        raise ValidationError(f"Invalid task id: {task_id}") from exc


def slug_from_manifest(manifest_path: Path) -> str:
    return manifest_path.stem


def phase_state_path(manifest_path: Path) -> Path:
    ensure_dir(BUILD_STATE_DIR)
    return BUILD_STATE_DIR / f"{slug_from_manifest(manifest_path)}.state.json"


def phase_summary_path(manifest_path: Path) -> Path:
    ensure_dir(BUILD_STATE_DIR)
    return BUILD_STATE_DIR / f"{slug_from_manifest(manifest_path)}.summary.md"


def task_attempt_dir(task_id: str, attempt: int) -> Path:
    return BUILD_STATE_DIR / "tasks" / task_id / f"attempt-{attempt:02d}"


def normalize_task_list(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValidationError("Manifest must contain a non-empty tasks list.")
    mapping: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValidationError("Each task must be an object.")
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValidationError("Every task must have a non-empty id.")
        if task_id in mapping:
            raise ValidationError(f"Duplicate task id: {task_id}")
        mapping[task_id] = task
    for task_id, task in mapping.items():
        for dep in task.get("dependencies", []):
            if dep not in mapping:
                raise ValidationError(f"Task {task_id} depends on unknown task {dep}.")
    return mapping


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") != MANIFEST_VERSION:
        raise ValidationError(f"Unsupported manifest schema: {manifest.get('schema_version')}")
    if manifest.get("project") != "kraken-bot-v4":
        raise ValidationError(f"Unexpected project: {manifest.get('project')}")

    phase = manifest.get("phase")
    if not isinstance(phase, dict):
        raise ValidationError("Manifest phase must be an object.")
    for key in ("id", "name", "spec_ref", "created_at", "created_by"):
        if not phase.get(key):
            raise ValidationError(f"Manifest phase missing {key}.")

    constraints = manifest.get("global_constraints")
    if not isinstance(constraints, dict):
        raise ValidationError("Manifest global_constraints must be an object.")
    for key in (
        "max_attempts_per_task",
        "max_files_changed_per_task",
        "module_line_cap",
        "typed_exceptions_only",
        "frozen_dataclasses_preferred",
        "no_git_push",
    ):
        if key not in constraints:
            raise ValidationError(f"Manifest global_constraints missing {key}.")

    tasks = normalize_task_list(manifest)
    for task_id, task in tasks.items():
        status = task.get("status")
        if status not in TASK_STATUSES:
            raise ValidationError(f"Task {task_id} has invalid status {status}.")
        if not isinstance(task.get("description"), str) or not task["description"].strip():
            raise ValidationError(f"Task {task_id} must have a description.")
        if not isinstance(task.get("acceptance_criteria"), list) or not task["acceptance_criteria"]:
            raise ValidationError(f"Task {task_id} must have acceptance_criteria.")
        if not isinstance(task.get("owned_paths"), list) or not task["owned_paths"]:
            raise ValidationError(f"Task {task_id} must have owned_paths.")
        if not isinstance(task.get("test_targets"), list):
            raise ValidationError(f"Task {task_id} must have test_targets.")
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    if path.suffix.lower() != ".json":
        raise ValidationError("Only JSON manifests are supported in v1.")
    if not path.exists():
        raise ValidationError(f"Manifest not found: {path}")
    return validate_manifest(read_json(path))


def initial_phase_state(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    tasks = normalize_task_list(manifest)
    task_states: dict[str, Any] = {}
    for task_id in sorted(tasks, key=natural_task_key):
        initial_status = "done" if tasks[task_id]["status"] == "done" else "pending"
        task_states[task_id] = {
            "status": initial_status,
            "attempt_count": 0,
            "resume_from": "start",
            "last_error": None,
            "commit_sha": None,
            "attempts": [],
        }
    return {
        "schema_version": STATE_VERSION,
        "manifest_path": rel_repo_path(manifest_path),
        "phase_id": manifest["phase"]["id"],
        "phase_name": manifest["phase"]["name"],
        "phase_status": "pending",
        "current_task_id": None,
        "updated_at": now_iso(),
        "tasks": task_states,
    }


def load_or_init_phase_state(manifest: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    state_path = phase_state_path(manifest_path)
    if not state_path.exists():
        state = initial_phase_state(manifest, manifest_path)
        atomic_write_json(state_path, state)
        return state
    state = read_json(state_path)
    validate_phase_state(state, manifest)
    return state


def validate_phase_state(state: dict[str, Any], manifest: dict[str, Any]) -> None:
    if state.get("schema_version") != STATE_VERSION:
        raise ValidationError(f"Unsupported phase state schema: {state.get('schema_version')}")
    if state.get("phase_status") not in PHASE_STATUSES:
        raise ValidationError(f"Invalid phase status: {state.get('phase_status')}")

    manifest_tasks = set(normalize_task_list(manifest))
    state_tasks = state.get("tasks")
    if not isinstance(state_tasks, dict):
        raise ValidationError("Phase state tasks must be an object.")
    if set(state_tasks) != manifest_tasks:
        raise ValidationError("Phase state task ids do not match manifest task ids.")

    for task_id, task_state in state_tasks.items():
        if task_state.get("status") not in TASK_STATUSES:
            raise ValidationError(f"Invalid status for task {task_id}: {task_state.get('status')}")
        if task_state.get("resume_from") not in {
            "start",
            "invoke-subagent",
            "pytest",
            "cc-review",
            "commit",
            "complete",
        }:
            raise ValidationError(f"Invalid resume_from for task {task_id}.")
        if not isinstance(task_state.get("attempt_count"), int) or task_state["attempt_count"] < 0:
            raise ValidationError(f"Invalid attempt_count for task {task_id}.")
        if not isinstance(task_state.get("attempts"), list):
            raise ValidationError(f"Invalid attempts list for task {task_id}.")


def save_phase_state(manifest_path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    atomic_write_json(phase_state_path(manifest_path), state)


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    timeout: int | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd or REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout,
        encoding="utf-8",
    )
    if check and result.returncode != 0:
        raise CommandError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result


def git_status(paths: list[str] | None = None) -> list[str]:
    args = ["git", "status", "--porcelain"]
    if paths:
        args.extend(["--", *paths])
    result = run_command(args, cwd=REPO_ROOT)
    entries: list[str] = []
    for raw_line in result.stdout.splitlines():
        if not raw_line.strip():
            continue
        path_text = raw_line[3:].strip()
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1].strip()
        entries.append(path_text.replace("\\", "/"))
    return entries


def git_diff(paths: list[str]) -> str:
    args = ["git", "diff", "--binary", "--no-ext-diff", "--", *paths]
    result = run_command(args, cwd=REPO_ROOT, check=False)
    return result.stdout


def git_stage(paths: list[str]) -> None:
    if paths:
        run_command(["git", "add", "--", *paths], cwd=REPO_ROOT)


def git_commit(message: str, paths: list[str], message_file: Path) -> str:
    if not paths:
        raise BuildError("Refusing to commit with no paths.")
    atomic_write_text(message_file, message)
    run_command(["git", "commit", "-F", str(message_file), "--", *paths], cwd=REPO_ROOT)
    return git_head_sha()


def git_head_sha() -> str:
    return run_command(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT).stdout.strip()


def changed_paths_for_owned_paths(owned_paths: list[str]) -> list[str]:
    return git_status(owned_paths)


def copy_file_if_exists(source: Path, target: Path) -> None:
    if source.exists():
        ensure_dir(target.parent)
        shutil.copy2(source, target)


def line_count_for_repo_file(repo_path: str) -> int | None:
    abs_path = REPO_ROOT / repo_path
    if not abs_path.exists() or abs_path.is_dir():
        return None
    return len(abs_path.read_text(encoding="utf-8").splitlines())


def ensure_cross_agent_runner() -> None:
    if not AGENT_RUNNER.exists():
        raise BuildError(f"Cross-agent runner not found: {AGENT_RUNNER}")


def which_or_none(binary: str) -> str | None:
    return shutil.which(binary)


def current_python() -> str:
    return sys.executable


def _cross_agent_call_ids() -> set[str]:
    if not CROSS_AGENT_STATE_DIR.exists():
        return set()
    call_ids: set[str] = set()
    for pattern in ("*.request.json", "*.result.json"):
        for path in CROSS_AGENT_STATE_DIR.glob(pattern):
            name = path.name
            if name.endswith(".request.json"):
                call_ids.add(name[: -len(".request.json")])
            elif name.endswith(".result.json"):
                call_ids.add(name[: -len(".result.json")])
    return call_ids


def _pick_latest_call_id(call_ids: set[str]) -> str | None:
    latest_id: str | None = None
    latest_mtime = -1.0
    for call_id in call_ids:
        for suffix in (".request.json", ".result.json"):
            path = CROSS_AGENT_STATE_DIR / f"{call_id}{suffix}"
            if path.exists():
                stat = path.stat()
                if stat.st_mtime > latest_mtime:
                    latest_id = call_id
                    latest_mtime = stat.st_mtime
    return latest_id


def normalize_cross_agent_result(payload: dict[str, Any], fallback_call_id: str | None = None) -> dict[str, Any]:
    if payload.get("protocol") == "cross-agent/v1" and isinstance(payload.get("result"), dict):
        normalized = dict(payload)
        normalized.setdefault("status", "failed")
        normalized["call_id"] = str(normalized.get("call_id") or fallback_call_id or "")
        return normalized

    notes_value = payload.get("notes", "")
    if isinstance(notes_value, list):
        notes_text = "; ".join(str(item) for item in notes_value)
    else:
        notes_text = str(notes_value)

    summary = str(payload.get("summary") or "")
    files_changed = payload.get("files_changed", [])
    if not isinstance(files_changed, list):
        files_changed = []
    status = str(payload.get("status") or "failed")
    has_completion_fields = bool(summary or files_changed)
    ok_field = payload.get("ok")
    is_completed = status == "completed" and (bool(ok_field) or ok_field is None) and has_completion_fields

    return {
        "protocol": "cross-agent/v1",
        "call_id": str(fallback_call_id or ""),
        "status": "completed" if is_completed else "failed",
        "result": {
            "summary": summary or "Subagent returned a non-standard success payload",
            "files_changed": [str(path) for path in files_changed],
            "answer": "",
            "confidence": 0.75 if is_completed else 0.0,
            "follow_up_needed": not is_completed,
            "notes": notes_text,
        },
        "execution": {
            "duration_seconds": 0,
            "tokens_used": None,
            "errors": [] if is_completed else [notes_text or "non-standard payload"],
        },
    }


def invoke_codex(prompt_text: str, owned_paths: list[str], timeout: int, dry_run: bool) -> tuple[dict[str, Any], str]:
    ensure_cross_agent_runner()
    before_call_ids = _cross_agent_call_ids()
    command = [
        current_python(),
        str(AGENT_RUNNER),
        "--direction",
        "cc-to-codex",
        "--task-type",
        "implement",
        "--prompt",
        prompt_text,
        "--working-dir",
        str(REPO_ROOT),
        "--owned-paths",
        *owned_paths,
        "--timeout",
        str(timeout),
    ]
    if dry_run:
        command.append("--dry-run")
    result = run_command(command, cwd=REPO_ROOT, check=False, timeout=timeout + 30)
    if dry_run:
        return {
            "protocol": "cross-agent/v1",
            "call_id": "dryrun000000",
            "status": "completed",
            "result": {
                "summary": "dry-run only",
                "files_changed": [],
                "answer": "",
                "confidence": 1.0,
                "follow_up_needed": False,
                "notes": "",
            },
            "execution": {"duration_seconds": 0, "tokens_used": None, "errors": []},
        }, result.stderr
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(
            f"Cross-agent runner returned non-JSON output.\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        ) from exc
    after_call_ids = _cross_agent_call_ids()
    fallback_call_id = _pick_latest_call_id(after_call_ids - before_call_ids)
    return normalize_cross_agent_result(payload, fallback_call_id=fallback_call_id), result.stderr


def copy_cross_agent_artifacts(call_id: str, attempt_dir: Path) -> None:
    if not call_id:
        return
    copy_file_if_exists(CROSS_AGENT_STATE_DIR / f"{call_id}.request.json", attempt_dir / "cross-agent.request.json")
    copy_file_if_exists(CROSS_AGENT_STATE_DIR / f"{call_id}.result.json", attempt_dir / "cross-agent.result.json")


def run_targeted_pytest(test_targets: list[str]) -> tuple[int, str]:
    if not test_targets:
        return 0, "No targeted pytest configured.\n"
    command = [current_python(), "-m", "pytest", "-q", *test_targets]
    result = run_command(command, cwd=REPO_ROOT, check=False)
    combined = result.stdout
    if result.stderr:
        combined = combined + ("\n" if combined and not combined.endswith("\n") else "") + result.stderr
    return result.returncode, combined


def review_decision_path(attempt_dir: Path) -> Path:
    return attempt_dir / "review.decision.json"


def write_review_example(path: Path) -> None:
    example = {
        "decision": "approve",
        "reason": "Acceptance criteria met and targeted pytest passed.",
    }
    example_path = path.with_name("review.decision.example.json")
    if not example_path.exists():
        atomic_write_json(example_path, example)


def invoke_codex_review(
    review_prompt: str,
    timeout: int = 120,
) -> dict[str, Any]:
    """Invoke Codex as a read-only reviewer via cross-agent.py."""
    ensure_cross_agent_runner()
    command = [
        current_python(),
        str(AGENT_RUNNER),
        "--direction",
        "cc-to-codex",
        "--task-type",
        "review",
        "--read-only",
        "--prompt",
        review_prompt,
        "--working-dir",
        str(REPO_ROOT),
        "--timeout",
        str(timeout),
    ]
    result = run_command(command, cwd=REPO_ROOT, check=False, timeout=timeout + 30)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "status": "failed",
            "result": {
                "summary": "Codex review returned non-JSON",
                "answer": result.stdout[:500],
                "confidence": 0.0,
                "follow_up_needed": True,
                "notes": result.stderr[:500],
                "files_changed": [],
            },
        }
    return normalize_cross_agent_result(payload)


def build_review_prompt(
    manifest: dict[str, Any],
    task: dict[str, Any],
    changed_files: list[str],
    pytest_exit: int,
    pytest_output: str,
) -> str:
    """Build a prompt for Codex to review a task implementation."""
    line_info = []
    cap = manifest["global_constraints"]["module_line_cap"]
    for path in changed_files:
        count = line_count_for_repo_file(path)
        if count is not None:
            flag = " OVER CAP" if count > cap else ""
            line_info.append(f"  - {path}: {count} lines{flag}")
        else:
            line_info.append(f"  - {path}: (new/unreadable)")

    lines = [
        f"Review task {task['id']} in phase {manifest['phase']['id']} {manifest['phase']['name']}.",
        "",
        f"Description: {task['description']}",
        "",
        "Acceptance criteria:",
        *[f"- {c}" for c in task["acceptance_criteria"]],
        "",
        "Changed files and line counts:",
        *line_info,
        "",
        f"Module line cap: {cap}",
        f"Pytest exit code: {pytest_exit}",
        "Pytest output (truncated to 2000 chars):",
        pytest_output[:2000],
        "",
        "Read each changed file. Check:",
        "1. Do the changes satisfy ALL acceptance criteria?",
        "2. Are there any broad except clauses (must be typed exceptions only)?",
        "3. Are frozen dataclasses used for state/value types?",
        "4. Is there dead code, obvious bugs, or missing edge cases?",
        "5. Does any file exceed the line cap?",
        "",
        "Respond with a JSON object in your answer field:",
        '{"verdict": "approve"} or {"verdict": "correct", "issues": ["issue1", "issue2"]}',
    ]
    return "\n".join(lines) + "\n"


def parse_codex_review_verdict(review_result: dict[str, Any]) -> tuple[str, list[str]]:
    """Parse Codex review result into (verdict, issues). Returns ('approve', []) or ('correct', [...])."""
    answer = review_result.get("result", {}).get("answer", "")
    if not answer:
        return "approve", []

    # Try to extract JSON verdict from the answer
    import re
    json_match = re.search(r'\{[^}]*"verdict"[^}]*\}', answer)
    if json_match:
        try:
            parsed = json.loads(json_match.group())
            verdict = parsed.get("verdict", "approve")
            issues = parsed.get("issues", [])
            if verdict in ("correct", "fail") and issues:
                return "correct", [str(i) for i in issues]
            if verdict == "approve":
                return "approve", []
        except json.JSONDecodeError:
            pass

    # Heuristic fallback: look for negative signals
    lower = answer.lower()
    if any(word in lower for word in ("fail", "reject", "critical", "broken", "missing")):
        return "correct", [f"Codex review flagged issues: {answer[:300]}"]

    return "approve", []


def load_review_decision(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    decision = read_json(path)
    if decision.get("decision") not in REVIEW_DECISIONS:
        raise ValidationError(f"Invalid review decision in {path}")
    if not isinstance(decision.get("reason"), str) or not decision["reason"].strip():
        raise ValidationError(f"Review decision reason required in {path}")
    must_fix = decision.get("must_fix", [])
    if must_fix and (not isinstance(must_fix, list) or not all(isinstance(item, str) for item in must_fix)):
        raise ValidationError(f"must_fix must be a list of strings in {path}")
    return decision


def prompt_for_task(
    manifest_path: Path,
    manifest: dict[str, Any],
    task: dict[str, Any],
    mode: str,
    decision: dict[str, Any] | None,
) -> str:
    lines = [
        f"Task ID: {task['id']}",
        f"Phase: {manifest['phase']['id']} {manifest['phase']['name']}",
        f"Repo: {REPO_ROOT.as_posix()}",
        "",
        "Read before editing:",
        f"- {rel_repo_path(REPO_ROOT / 'SPEC.md')} -> {manifest['phase']['spec_ref']}",
        f"- {rel_repo_path(manifest_path)} -> task {task['id']}",
        "",
    ]
    if mode == "correct" and decision:
        lines.extend(
            [
                "Attempt: correction",
                "",
                "Keep the valid parts of the current implementation intact unless the review explicitly says otherwise.",
                "",
                "Why the prior attempt was rejected:",
                f"- {decision['reason']}",
            ]
        )
        for must_fix in decision.get("must_fix", []):
            lines.append(f"- MUST FIX: {must_fix}")
        lines.append("")
        lines.append("Required fixes:")
        for index, item in enumerate(decision.get("must_fix", []) or [decision["reason"]], start=1):
            prefix = f"{index}." if index == 1 else "-"
            lines.append(f"{prefix} {item}")
        lines.append("")
    else:
        lines.extend([f"Implement this task: {task['description']}", ""])

    lines.extend(["Acceptance criteria:"])
    for criterion in task["acceptance_criteria"]:
        lines.append(f"- {criterion}")
    lines.extend(
        [
            "",
            "Owned paths:",
            *[f"- {path}" for path in task["owned_paths"]],
            "",
            "Constraints:",
            "- Python only.",
            f"- Keep each file under {manifest['global_constraints']['module_line_cap']} lines.",
            "- Use typed exceptions only. No broad except Exception.",
            "- Prefer frozen dataclasses where state/value objects are introduced.",
            "- Do not run tests, lint, or git commands.",
            "- Do not modify files outside owned paths.",
            "- Keep implementation as small as possible for this task only.",
            "",
            "Claude Code will verify with:",
        ]
    )
    if task["test_targets"]:
        for target in task["test_targets"]:
            lines.append(f"- {current_python()} -m pytest -q {target}")
    else:
        lines.append("- No targeted pytest for this task; diff review only.")
    lines.extend(["", "Return a normal cross-agent result JSON and exit."])
    return "\n".join(lines) + "\n"


def build_review_packet(
    manifest: dict[str, Any],
    task: dict[str, Any],
    runner_result: dict[str, Any],
    changed_files: list[str],
    pytest_exit: int,
    pytest_output: str,
) -> str:
    lines = [
        f"# Review Packet: Task {task['id']}",
        "",
        f"Phase: {manifest['phase']['id']} {manifest['phase']['name']}",
        f"Description: {task['description']}",
        "",
        "Acceptance criteria:",
    ]
    for item in task["acceptance_criteria"]:
        lines.append(f"- {item}")
    lines.extend(["", "Changed files:"])
    if changed_files:
        for path in changed_files:
            count = line_count_for_repo_file(path)
            suffix = "" if count is None else f" ({count} lines)"
            lines.append(f"- {path}{suffix}")
            if count is not None and count > manifest["global_constraints"]["module_line_cap"]:
                lines.append(f"  WARNING: exceeds {manifest['global_constraints']['module_line_cap']} line cap")
    else:
        lines.append("- No files changed.")

    lines.extend(
        [
            "",
            f"Codex summary: {runner_result.get('result', {}).get('summary', '')}",
            f"Runner status: {runner_result.get('status')}",
            "",
            f"Targeted pytest exit code: {pytest_exit}",
            "Targeted pytest output:",
            "```text",
            pytest_output.rstrip(),
            "```",
            "",
            "Write review.decision.json in this directory with one of:",
            '- {"decision": "approve", "reason": "..." }',
            '- {"decision": "correct", "reason": "...", "must_fix": ["...", "..."] }',
            '- {"decision": "fail", "reason": "..." }',
            "",
        ]
    )
    return "\n".join(lines).strip() + "\n"


def commit_message(
    manifest_path: Path,
    manifest: dict[str, Any],
    task: dict[str, Any],
    attempt: dict[str, Any],
) -> str:
    subject = f"build(phase-{manifest['phase']['id']}/{task['id']}): {task['description']}"
    tests = " ".join(f"{current_python()} -m pytest -q {target}" for target in task["test_targets"]) or "none"
    body = [
        f"Phase: {manifest['phase']['name']}",
        f"Task: {task['id']}",
        f"Manifest: {rel_repo_path(manifest_path)}",
        f"Attempt: {attempt['attempt']}/{manifest['global_constraints']['max_attempts_per_task']}",
        f"Tests: {tests}",
        f"Call-ID: {attempt.get('call_id') or 'unknown'}",
        "Verified-By: Claude Code diff review + targeted pytest",
    ]
    return subject + "\n\n" + "\n".join(body) + "\n"
