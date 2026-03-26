from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from beliefs.orchestrator import BeliefCycleResult, BeliefOrchestrator
from core.types import BeliefDirection, BeliefSnapshot, BeliefSource, MarketRegime


def make_snapshot(
    source: BeliefSource,
    direction: BeliefDirection,
    confidence: float,
    regime: MarketRegime = MarketRegime.UNKNOWN,
) -> BeliefSnapshot:
    return BeliefSnapshot(
        pair="DOGE/USD",
        direction=direction,
        confidence=confidence,
        regime=regime,
        sources=(source,),
    )


def make_source(snapshot: BeliefSnapshot | None) -> Mock:
    source = Mock()
    source.analyze.return_value = snapshot
    return source


def test_belief_cycle_result_is_frozen() -> None:
    result = BeliefCycleResult(
        pair="DOGE/USD",
        source_beliefs={},
        consensus=Mock(),
        timestamp=datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc),
        stale=False,
    )

    with pytest.raises(FrozenInstanceError):
        result.pair = "BTC/USD"


def test_run_belief_cycle_returns_consensus_when_all_sources_agree() -> None:
    timestamp = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)
    claude = make_source(
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 0.9, MarketRegime.TRENDING)
    )
    codex = make_source(
        make_snapshot(BeliefSource.CODEX, BeliefDirection.BULLISH, 0.8, MarketRegime.TRENDING)
    )
    technical_ensemble = make_source(
        make_snapshot(
            BeliefSource.TECHNICAL_ENSEMBLE,
            BeliefDirection.BULLISH,
            0.7,
            MarketRegime.RANGING,
        )
    )
    orchestrator = BeliefOrchestrator(
        {
            BeliefSource.CLAUDE: claude,
            BeliefSource.CODEX: codex,
            BeliefSource.TECHNICAL_ENSEMBLE: technical_ensemble,
        },
        clock=lambda: timestamp,
    )

    result = orchestrator.run_belief_cycle("DOGE/USD")

    assert result.pair == "DOGE/USD"
    assert result.timestamp == timestamp
    assert result.stale is False
    assert result.source_beliefs[BeliefSource.CLAUDE] is not None
    assert result.source_beliefs[BeliefSource.CODEX] is not None
    assert result.source_beliefs[BeliefSource.TECHNICAL_ENSEMBLE] is not None
    assert result.consensus.agreed_direction is BeliefDirection.BULLISH
    assert result.consensus.agreement_count == 3
    assert result.consensus.total_sources == 3
    assert result.consensus.strength_score == pytest.approx(0.8)
    assert result.consensus.regime is MarketRegime.TRENDING

    claude.analyze.assert_called_once_with("DOGE/USD")
    codex.analyze.assert_called_once_with("DOGE/USD")
    technical_ensemble.analyze.assert_called_once_with("DOGE/USD")


def test_run_belief_cycle_returns_two_of_three_consensus() -> None:
    claude = make_source(
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 1.0, MarketRegime.TRENDING)
    )
    codex = make_source(
        make_snapshot(BeliefSource.CODEX, BeliefDirection.BULLISH, 0.5, MarketRegime.RANGING)
    )
    technical_ensemble = make_source(
        make_snapshot(
            BeliefSource.TECHNICAL_ENSEMBLE,
            BeliefDirection.BEARISH,
            0.4,
            MarketRegime.TRENDING,
        )
    )
    orchestrator = BeliefOrchestrator(
        {
            "claude": claude,
            "codex": codex,
            "technical_ensemble": technical_ensemble,
        }
    )

    result = orchestrator.run_belief_cycle("DOGE/USD")

    assert result.consensus.agreed_direction is BeliefDirection.BULLISH
    assert result.consensus.agreement_count == 2
    assert result.consensus.total_sources == 3
    assert result.consensus.strength_score == pytest.approx(0.5)
    assert result.consensus.regime is MarketRegime.TRENDING


def test_run_belief_cycle_is_inconclusive_when_all_sources_fail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    claude = make_source(None)
    codex = make_source(None)
    technical_ensemble = make_source(None)
    orchestrator = BeliefOrchestrator(
        {
            BeliefSource.CLAUDE: claude,
            BeliefSource.CODEX: codex,
            BeliefSource.TECHNICAL_ENSEMBLE: technical_ensemble,
        }
    )

    with caplog.at_level("WARNING"):
        result = orchestrator.run_belief_cycle("DOGE/USD")

    assert result.source_beliefs[BeliefSource.CLAUDE] is None
    assert result.source_beliefs[BeliefSource.CODEX] is None
    assert result.source_beliefs[BeliefSource.TECHNICAL_ENSEMBLE] is None
    assert result.consensus.agreed_direction is BeliefDirection.NEUTRAL
    assert result.consensus.agreement_count == 0
    assert result.consensus.total_sources == 0
    assert result.consensus.strength_score == pytest.approx(0.0)
    assert result.consensus.regime is MarketRegime.UNKNOWN
    assert "claude" in caplog.text
    assert "codex" in caplog.text
    assert "technical_ensemble" in caplog.text


def test_run_belief_cycle_excludes_failed_sources_from_consensus_and_routes_inputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    timestamp = datetime(2026, 3, 24, 18, 0, tzinfo=timezone.utc)
    claude = make_source(
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 0.8, MarketRegime.TRENDING)
    )
    codex = make_source(None)
    technical_ensemble = make_source(
        make_snapshot(
            BeliefSource.TECHNICAL_ENSEMBLE,
            BeliefDirection.BULLISH,
            0.6,
            MarketRegime.RANGING,
        )
    )
    orchestrator = BeliefOrchestrator(
        {
            BeliefSource.CLAUDE: claude,
            BeliefSource.CODEX: codex,
            BeliefSource.TECHNICAL_ENSEMBLE: technical_ensemble,
        },
        clock=lambda: timestamp,
    )

    with caplog.at_level("WARNING"):
        result = orchestrator.run_belief_cycle(
            "DOGE/USD",
            source_inputs={
                BeliefSource.CLAUDE: {"recent_trade_history_summary": "Two wins."},
                BeliefSource.CODEX: {"recent_trade_history_summary": "Two wins."},
                BeliefSource.TECHNICAL_ENSEMBLE: {"bars": "mock-bars"},
            },
        )

    assert result.timestamp == timestamp
    assert result.stale is False
    assert result.source_beliefs[BeliefSource.CODEX] is None
    assert len(result.valid_beliefs) == 2
    assert result.consensus.agreed_direction is BeliefDirection.BULLISH
    assert result.consensus.agreement_count == 2
    assert result.consensus.total_sources == 2
    assert result.consensus.strength_score == pytest.approx(0.7)
    assert result.consensus.regime is MarketRegime.UNKNOWN
    assert "codex" in caplog.text

    claude.analyze.assert_called_once_with(
        "DOGE/USD",
        recent_trade_history_summary="Two wins.",
    )
    codex.analyze.assert_called_once_with(
        "DOGE/USD",
        recent_trade_history_summary="Two wins.",
    )
    technical_ensemble.analyze.assert_called_once_with(
        "DOGE/USD",
        bars="mock-bars",
    )
