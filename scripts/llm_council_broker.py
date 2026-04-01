"""LLM Council Broker — polls for request files, dispatches to CC+Codex panes.

Run as a sidecar alongside the bot:
    python scripts/llm_council_broker.py [--council-dir state/llm-council] [--interval 30]

The broker:
1. Watches requests/ for unprocessed .json files
2. Validates tmux panes before dispatch (health checks)
3. Sends structured prompts to CC and Codex tmux panes (with retry)
4. Waits for response files (with timeout)
5. Computes consensus with valid-vote tracking and writes consensus/ file
6. Cleans up stale files on startup
7. Never touches bot state directly
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path when run as a script
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from beliefs.llm_council_protocol import (
    CouncilConsensus,
    CouncilRequest,
    CouncilVote,
    compute_consensus,
    council_paths,
)

logger = logging.getLogger(__name__)

RESPONSE_TIMEOUT_SEC: Final[int] = 120
POLL_INTERVAL_SEC: Final[int] = 30
AGENTS: Final[tuple[str, str]] = ("claude", "codex")
SEND_RETRY_DELAY_SEC: Final[int] = 5
STALE_CLEANUP_HOURS: Final[int] = 24

# Default pane targets — override via env or args
DEFAULT_CLAUDE_PANE: Final[str] = "work:2.1"
DEFAULT_CODEX_PANE: Final[str] = "work:2.0"


# ---------------------------------------------------------------------------
# Pane health checks
# ---------------------------------------------------------------------------

def _pane_exists(pane: str) -> bool:
    """Check whether a tmux pane target exists (session + pane)."""
    session = pane.split(":")[0]
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", session],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        result = subprocess.run(
            ["tmux", "list-panes", "-t", pane, "-F", "#{pane_id}"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0 and len(result.stdout.strip()) > 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _pane_is_ready(pane: str) -> bool:
    """Heuristic: pane exists and is not in copy-mode or alternate screen."""
    if not _pane_exists(pane):
        return False
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", pane, "-p", "#{pane_in_mode}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return True  # Can't check mode; assume ready
        return result.stdout.strip() == "0"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True  # tmux unavailable; best-effort


def _peek_pane(pane: str, lines: int = 5) -> str:
    """Capture the last N visible lines from a tmux pane."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane, "-p", "-S", str(-lines)],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# Stale file cleanup
# ---------------------------------------------------------------------------

def _cleanup_stale_files(paths: dict[str, Path], max_age_hours: int = STALE_CLEANUP_HOURS) -> int:
    """Remove request and response files older than max_age_hours.

    Consensus files are kept for history/debugging.
    """
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for subdir in ("requests", "responses"):
        for f in paths[subdir].glob("*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Cleaned up %d stale files (older than %dh)", removed, max_age_hours)
    return removed


# ---------------------------------------------------------------------------
# Main broker loop
# ---------------------------------------------------------------------------

def run_broker(
    council_dir: Path,
    *,
    claude_pane: str = DEFAULT_CLAUDE_PANE,
    codex_pane: str = DEFAULT_CODEX_PANE,
    poll_interval: int = POLL_INTERVAL_SEC,
    cleanup_hours: int = STALE_CLEANUP_HOURS,
) -> None:
    """Main broker loop — poll for requests, dispatch, collect, consensus."""
    paths = council_paths(council_dir)
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)

    panes = {"claude": claude_pane, "codex": codex_pane}

    # Startup: clean stale files and validate panes
    _cleanup_stale_files(paths, cleanup_hours)
    for agent_name, pane_target in panes.items():
        if _pane_exists(pane_target):
            logger.info("Pane %s (%s): OK", pane_target, agent_name)
        else:
            logger.warning("Pane %s (%s): NOT FOUND — will retry each dispatch", pane_target, agent_name)

    logger.info(
        "LLM Council broker started (dir=%s, claude=%s, codex=%s)",
        council_dir, claude_pane, codex_pane,
    )

    while True:
        try:
            _process_pending_requests(paths, panes)
        except Exception as exc:
            logger.error("Broker cycle error: %s", exc)
        time.sleep(poll_interval)


def _process_pending_requests(
    paths: dict[str, Path],
    panes: dict[str, str],
) -> None:
    """Process all pending request files."""
    for request_file in sorted(paths["requests"].glob("*.json")):
        call_id = request_file.stem
        consensus_file = paths["consensus"] / f"{call_id}.json"

        if consensus_file.exists():
            continue  # Already processed

        try:
            raw = request_file.read_text(encoding="utf-8")
            request = CouncilRequest.from_json(raw)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Deleting malformed request %s: %s", call_id, exc)
            request_file.unlink(missing_ok=True)
            continue

        logger.info("Processing council request %s for %s", call_id, request.pair)
        _dispatch_and_collect(request, paths, panes)


def _dispatch_and_collect(
    request: CouncilRequest,
    paths: dict[str, Path],
    panes: dict[str, str],
) -> None:
    """Send request to both agents, collect responses, write consensus."""
    call_id = request.call_id

    # Send to both panes (with health checks and retry)
    for agent in AGENTS:
        response_path = paths["responses"] / f"{call_id}.{agent}.json"
        if response_path.exists():
            continue  # Already have this agent's response

        pane = panes.get(agent)
        if not pane:
            continue

        # Health check
        if not _pane_exists(pane):
            logger.warning("Pane %s for %s does not exist, skipping", pane, agent)
            continue

        if not _pane_is_ready(pane):
            logger.info("Pane %s for %s in copy-mode/alt-screen, sending anyway", pane, agent)

        prompt = _build_agent_prompt(request, agent, response_path)
        sent = False
        for attempt in range(2):  # 1 try + 1 retry
            try:
                _send_to_pane(pane, prompt)
                logger.info("Sent request to %s pane %s (attempt %d)", agent, pane, attempt + 1)
                sent = True
                break
            except Exception as exc:
                logger.warning("Send to %s failed (attempt %d): %s", agent, attempt + 1, exc)
                if attempt == 0:
                    time.sleep(SEND_RETRY_DELAY_SEC)

        if not sent:
            logger.error("Could not send to %s after 2 attempts, skipping", agent)

    # Wait for responses — track valid parsed votes separately
    votes: list[CouncilVote] = []
    vote_details: dict[str, dict] = {}
    valid_vote_count = 0

    deadline = time.monotonic() + RESPONSE_TIMEOUT_SEC
    while time.monotonic() < deadline:
        all_done = True
        for agent in AGENTS:
            response_path = paths["responses"] / f"{call_id}.{agent}.json"
            if not response_path.exists():
                all_done = False
                continue

            if agent not in vote_details:
                try:
                    raw = response_path.read_text(encoding="utf-8")
                    vote = CouncilVote.from_json(raw)
                    votes.append(vote)
                    vote_details[agent] = {
                        "direction": vote.direction,
                        "confidence": vote.confidence,
                        "regime": vote.regime,
                    }
                    valid_vote_count += 1
                    logger.info(
                        "Got %s vote: %s (conf=%.2f)",
                        agent, vote.direction, vote.confidence,
                    )
                except Exception as exc:
                    logger.warning("Invalid response from %s: %s", agent, exc)
                    # File exists but is malformed — mark as seen but not valid
                    vote_details[agent] = {"direction": "neutral", "confidence": 0.0, "regime": "unknown"}

        if all_done or len(vote_details) == len(AGENTS):
            break
        time.sleep(5)

    # Determine status based on valid votes (not just file presence)
    if valid_vote_count == 0:
        status = "failed"
    elif valid_vote_count < len(AGENTS):
        status = "partial"
    else:
        status = "completed"

    # Compute consensus
    direction, confidence, regime = compute_consensus(votes)
    now = datetime.now(timezone.utc)

    consensus = CouncilConsensus(
        call_id=call_id,
        pair=request.pair,
        as_of=request.as_of,
        status=status,
        votes=vote_details,
        direction=direction,
        confidence=confidence,
        regime=regime,
        completed_at=now.isoformat(),
        valid_vote_count=valid_vote_count,
        expected_vote_count=len(AGENTS),
    )

    consensus_path = paths["consensus"] / f"{call_id}.json"
    consensus_path.write_text(consensus.to_json(), encoding="utf-8")
    logger.info(
        "Council consensus for %s: %s (conf=%.2f, status=%s, %d/%d valid votes)",
        request.pair, direction, confidence, status, valid_vote_count, len(AGENTS),
    )


def _build_agent_prompt(
    request: CouncilRequest,
    agent: str,
    response_path: Path,
) -> str:
    """Build a compact single-line prompt for an agent pane.

    The prompt is collapsed to one line by _send_to_pane, so we keep it
    concise and avoid unnecessary formatting.
    """
    context_json = json.dumps(request.context, separators=(",", ":"), default=str)
    response_file = str(response_path.resolve())
    return (
        f"LLM_COUNCIL_REQUEST {request.call_id} — "
        f"You are a market analyst for {request.pair}. "
        f"Market context: {context_json}. "
        f"Analyze and write your response as JSON to: {response_file}. "
        f'The JSON must have these exact keys: '
        f'{{"schema_version":"llm-council/v1","call_id":"{request.call_id}",'
        f'"agent":"{agent}","direction":"bullish or bearish or neutral",'
        f'"confidence":float 0.0-1.0,"regime":"trending or ranging or unknown",'
        f'"reasoning":"1-2 sentence explanation"}}. '
        f"After writing the file, print: LLM_COUNCIL_DONE {request.call_id}"
    )


def _send_to_pane(pane: str, text: str) -> None:
    """Send text to a tmux pane via tmux send-keys.

    Collapses the prompt to a single line to avoid multi-line paste issues
    in CLI tools (Claude Code, Codex). Sends Enter twice with a short delay
    to ensure submission in interactive prompts that buffer input.
    """
    # Collapse to single line — CLI agents handle long single-line prompts fine
    one_line = " ".join(text.split())
    escaped = one_line.replace("'", "'\\''")
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, escaped, "Enter"],
        check=True,
        timeout=10,
    )
    # Extra Enter after brief delay — some CLIs need a second press to submit
    time.sleep(0.5)
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, "Enter"],
        check=True,
        timeout=5,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM Council Broker")
    parser.add_argument(
        "--council-dir", type=Path, default=Path("state/llm-council"),
    )
    parser.add_argument("--claude-pane", default=DEFAULT_CLAUDE_PANE)
    parser.add_argument("--codex-pane", default=DEFAULT_CODEX_PANE)
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SEC)
    parser.add_argument("--once", action="store_true", help="Process once and exit")
    parser.add_argument("--cleanup-hours", type=int, default=STALE_CLEANUP_HOURS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.once:
        paths = council_paths(args.council_dir)
        for p in paths.values():
            p.mkdir(parents=True, exist_ok=True)
        _cleanup_stale_files(paths, args.cleanup_hours)
        panes = {"claude": args.claude_pane, "codex": args.codex_pane}
        _process_pending_requests(paths, panes)
    else:
        run_broker(
            args.council_dir,
            claude_pane=args.claude_pane,
            codex_pane=args.codex_pane,
            poll_interval=args.interval,
            cleanup_hours=args.cleanup_hours,
        )


if __name__ == "__main__":
    main()
