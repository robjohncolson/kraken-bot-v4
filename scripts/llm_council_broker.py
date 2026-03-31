"""LLM Council Broker — polls for request files, dispatches to CC+Codex panes.

Run as a sidecar alongside the bot:
    python scripts/llm_council_broker.py [--council-dir state/llm-council] [--interval 30]

The broker:
1. Watches requests/ for unprocessed .json files
2. Sends structured prompts to CC and Codex tmux panes
3. Waits for response files (with timeout)
4. Computes consensus and writes consensus/ file
5. Never touches bot state directly
"""

from __future__ import annotations

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

# Default pane targets — override via env or args
DEFAULT_CLAUDE_PANE: Final[str] = "work:2.1"
DEFAULT_CODEX_PANE: Final[str] = "work:2.0"


def run_broker(
    council_dir: Path,
    *,
    claude_pane: str = DEFAULT_CLAUDE_PANE,
    codex_pane: str = DEFAULT_CODEX_PANE,
    poll_interval: int = POLL_INTERVAL_SEC,
) -> None:
    """Main broker loop — poll for requests, dispatch, collect, consensus."""
    paths = council_paths(council_dir)
    panes = {"claude": claude_pane, "codex": codex_pane}

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
            logger.warning("Skipping malformed request %s: %s", call_id, exc)
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

    # Send to both panes
    for agent in AGENTS:
        response_path = paths["responses"] / f"{call_id}.{agent}.json"
        if response_path.exists():
            continue  # Already have this agent's response

        pane = panes.get(agent)
        if not pane:
            continue

        prompt = _build_agent_prompt(request, agent, response_path)
        try:
            _send_to_pane(pane, prompt)
            logger.info("Sent request to %s pane %s", agent, pane)
        except Exception as exc:
            logger.warning("Failed to send to %s: %s", agent, exc)

    # Wait for responses
    votes: list[CouncilVote] = []
    vote_details: dict[str, dict] = {}

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
                    logger.info(
                        "Got %s vote: %s (conf=%.2f)",
                        agent, vote.direction, vote.confidence,
                    )
                except Exception as exc:
                    logger.warning("Invalid response from %s: %s", agent, exc)
                    vote_details[agent] = {"direction": "neutral", "confidence": 0.0, "regime": "unknown"}

        if all_done or len(vote_details) == len(AGENTS):
            break
        time.sleep(5)

    # Compute consensus
    direction, confidence, regime = compute_consensus(votes)
    now = datetime.now(timezone.utc)

    consensus = CouncilConsensus(
        call_id=call_id,
        pair=request.pair,
        as_of=request.as_of,
        status="completed" if len(vote_details) == len(AGENTS) else "partial",
        votes=vote_details,
        direction=direction,
        confidence=confidence,
        regime=regime,
        completed_at=now.isoformat(),
    )

    consensus_path = paths["consensus"] / f"{call_id}.json"
    consensus_path.write_text(consensus.to_json(), encoding="utf-8")
    logger.info(
        "Council consensus for %s: %s (conf=%.2f, %d/%d votes)",
        request.pair, direction, confidence, len(vote_details), len(AGENTS),
    )


def _build_agent_prompt(
    request: CouncilRequest,
    agent: str,
    response_path: Path,
) -> str:
    """Build the prompt to send to an agent pane."""
    context_json = json.dumps(request.context, indent=2, default=str)
    return (
        f"LLM_COUNCIL_REQUEST {request.call_id}\n"
        f"You are acting as a market analyst for a DOGE/USD trading bot.\n"
        f"Analyze this market context and write your response as JSON to:\n"
        f"  {response_path.resolve()}\n\n"
        f"Market context:\n{context_json}\n\n"
        f"Write EXACTLY this JSON schema to the file:\n"
        f'{{"schema_version": "llm-council/v1", "call_id": "{request.call_id}", '
        f'"agent": "{agent}", "direction": "bullish|bearish|neutral", '
        f'"confidence": 0.0-1.0, "regime": "trending|ranging|unknown", '
        f'"reasoning": "1-2 sentence explanation"}}\n\n'
        f"After writing the file, print: LLM_COUNCIL_DONE {request.call_id}"
    )


def _send_to_pane(pane: str, text: str) -> None:
    """Send text to a tmux pane via tmux send-keys."""
    # Use tmux directly — the broker runs outside of MCP context
    escaped = text.replace("'", "'\\''")
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, escaped, "Enter"],
        check=True,
        timeout=10,
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.once:
        paths = council_paths(args.council_dir)
        panes = {"claude": args.claude_pane, "codex": args.codex_pane}
        _process_pending_requests(paths, panes)
    else:
        run_broker(
            args.council_dir,
            claude_pane=args.claude_pane,
            codex_pane=args.codex_pane,
            poll_interval=args.interval,
        )


if __name__ == "__main__":
    main()
