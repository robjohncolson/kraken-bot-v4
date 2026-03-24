from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from beliefs.claude_source import ClaudeSource
from beliefs.prompts import build_belief_prompt
from core.types import BeliefDirection, BeliefSource, MarketRegime


def test_analyze_invokes_claude_cli_and_parses_json_response() -> None:
    source = ClaudeSource(timeout_seconds=45, last_n_closed_positions=7)
    response = {
        "type": "result",
        "subtype": "success",
        "result": json.dumps(
            {
                "direction": "bullish",
                "confidence": 0.82,
                "regime": "trending",
            }
        ),
    }
    expected_prompt = build_belief_prompt(
        pair="DOGE/USD",
        timeframe="4h",
        recent_trade_history_summary="Two wins, one stop-out.",
        last_n_closed_positions=7,
    )

    with patch(
        "beliefs.claude_source.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(response),
            stderr="",
        ),
    ) as run_mock:
        snapshot = source.analyze(
            pair="DOGE/USD",
            timeframe="4h",
            recent_trade_history_summary="Two wins, one stop-out.",
        )

    assert snapshot is not None
    assert snapshot.pair == "DOGE/USD"
    assert snapshot.direction is BeliefDirection.BULLISH
    assert snapshot.confidence == 0.82
    assert snapshot.regime is MarketRegime.TRENDING
    assert snapshot.sources == (BeliefSource.CLAUDE,)

    run_mock.assert_called_once()
    assert run_mock.call_args.args[0] == [
        "claude",
        "-p",
        expected_prompt,
        "--output-format",
        "json",
    ]
    assert run_mock.call_args.kwargs["timeout"] == 45
    assert run_mock.call_args.kwargs["capture_output"] is True
    assert run_mock.call_args.kwargs["text"] is True


def test_analyze_returns_none_for_malformed_json_response() -> None:
    source = ClaudeSource()
    malformed = {
        "type": "result",
        "result": json.dumps(
            {
                "direction": "sideways",
                "confidence": "high",
                "regime": "volatile",
            }
        ),
    }

    with patch(
        "beliefs.claude_source.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout=json.dumps(malformed),
            stderr="",
        ),
    ):
        snapshot = source.analyze(
            pair="DOGE/USD",
            recent_trade_history_summary="No recent trades.",
        )

    assert snapshot is None


def test_analyze_returns_none_when_cli_exits_non_zero() -> None:
    source = ClaudeSource()

    with patch(
        "beliefs.claude_source.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["claude"],
            returncode=1,
            stdout="",
            stderr="failed",
        ),
    ):
        snapshot = source.analyze(
            pair="DOGE/USD",
            recent_trade_history_summary="No recent trades.",
        )

    assert snapshot is None


def test_analyze_returns_none_when_cli_times_out() -> None:
    source = ClaudeSource(timeout_seconds=12)

    with patch(
        "beliefs.claude_source.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=12),
    ):
        snapshot = source.analyze(
            pair="DOGE/USD",
            recent_trade_history_summary="No recent trades.",
        )

    assert snapshot is None
