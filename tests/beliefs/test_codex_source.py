from __future__ import annotations

import subprocess
from unittest.mock import patch

from beliefs.codex_source import CodexSource
from beliefs.prompts import build_belief_prompt
from core.types import BeliefDirection, BeliefSource, MarketRegime


def test_analyze_invokes_codex_cli_and_parses_response() -> None:
    source = CodexSource(timeout_seconds=45, last_n_closed_positions=7)
    expected_prompt = build_belief_prompt(
        pair="DOGE/USD",
        timeframe="4h",
        recent_trade_history_summary="Two wins, one stop-out.",
        last_n_closed_positions=7,
    )

    with patch(
        "beliefs.codex_source.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="Direction: bearish\nConfidence: 0.61\nRegime: ranging\n",
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
    assert snapshot.direction is BeliefDirection.BEARISH
    assert snapshot.confidence == 0.61
    assert snapshot.regime is MarketRegime.RANGING
    assert snapshot.sources == (BeliefSource.CODEX,)

    run_mock.assert_called_once()
    assert run_mock.call_args.args[0] == ["codex", "exec", "--full-auto", "-"]
    assert run_mock.call_args.kwargs["input"] == expected_prompt
    assert run_mock.call_args.kwargs["timeout"] == 45
    assert run_mock.call_args.kwargs["capture_output"] is True
    assert run_mock.call_args.kwargs["text"] is True


def test_analyze_returns_none_for_unparseable_response() -> None:
    source = CodexSource()

    with patch(
        "beliefs.codex_source.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["codex"],
            returncode=0,
            stdout="Direction: sideways\nConfidence: high\nRegime: volatile\n",
            stderr="",
        ),
    ):
        snapshot = source.analyze(
            pair="DOGE/USD",
            recent_trade_history_summary="No recent trades.",
        )

    assert snapshot is None


def test_analyze_returns_none_when_cli_exits_non_zero() -> None:
    source = CodexSource()

    with patch(
        "beliefs.codex_source.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["codex"],
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
    source = CodexSource(timeout_seconds=12)

    with patch(
        "beliefs.codex_source.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["codex"], timeout=12),
    ):
        snapshot = source.analyze(
            pair="DOGE/USD",
            recent_trade_history_summary="No recent trades.",
        )

    assert snapshot is None
