from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from beliefs.consensus import ConsensusResult, compute_consensus
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


def test_consensus_result_is_frozen() -> None:
    result = ConsensusResult(
        agreed_direction=BeliefDirection.NEUTRAL,
        agreement_count=0,
        total_sources=0,
        strength_score=0.0,
        regime=MarketRegime.UNKNOWN,
    )

    with pytest.raises(FrozenInstanceError):
        result.total_sources = 1


def test_compute_consensus_returns_unanimous_agreement() -> None:
    snapshots = [
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 1.0, MarketRegime.TRENDING),
        make_snapshot(BeliefSource.CODEX, BeliefDirection.BULLISH, 1.0, MarketRegime.TRENDING),
        make_snapshot(
            BeliefSource.TECHNICAL_ENSEMBLE,
            BeliefDirection.BULLISH,
            1.0,
            MarketRegime.RANGING,
        ),
    ]

    result = compute_consensus(snapshots)

    assert result.agreed_direction is BeliefDirection.BULLISH
    assert result.agreement_count == 3
    assert result.total_sources == 3
    assert result.strength_score == pytest.approx(1.0)
    assert result.regime is MarketRegime.TRENDING


def test_compute_consensus_returns_two_of_three_split() -> None:
    snapshots = [
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 1.0, MarketRegime.TRENDING),
        make_snapshot(BeliefSource.CODEX, BeliefDirection.BULLISH, 1.0, MarketRegime.RANGING),
        make_snapshot(
            BeliefSource.TECHNICAL_ENSEMBLE,
            BeliefDirection.BEARISH,
            1.0,
            MarketRegime.TRENDING,
        ),
    ]

    result = compute_consensus(snapshots)

    assert result.agreed_direction is BeliefDirection.BULLISH
    assert result.agreement_count == 2
    assert result.total_sources == 3
    assert result.strength_score == pytest.approx(0.67)
    assert result.regime is MarketRegime.TRENDING


def test_compute_consensus_returns_neutral_for_three_way_split() -> None:
    snapshots = [
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 1.0, MarketRegime.TRENDING),
        make_snapshot(BeliefSource.CODEX, BeliefDirection.BEARISH, 1.0, MarketRegime.RANGING),
        make_snapshot(BeliefSource.TECHNICAL_ENSEMBLE, BeliefDirection.NEUTRAL, 1.0),
    ]

    result = compute_consensus(snapshots)

    assert result.agreed_direction is BeliefDirection.NEUTRAL
    assert result.agreement_count == 1
    assert result.total_sources == 3
    assert result.strength_score == pytest.approx(0.0)
    assert result.regime is MarketRegime.UNKNOWN


def test_compute_consensus_returns_single_source_view() -> None:
    result = compute_consensus(
        [
            make_snapshot(
                BeliefSource.CLAUDE,
                BeliefDirection.BEARISH,
                0.4,
                MarketRegime.RANGING,
            )
        ]
    )

    assert result.agreed_direction is BeliefDirection.BEARISH
    assert result.agreement_count == 1
    assert result.total_sources == 1
    assert result.strength_score == pytest.approx(0.4)
    assert result.regime is MarketRegime.RANGING


def test_compute_consensus_returns_neutral_for_empty_input() -> None:
    result = compute_consensus([])

    assert result.agreed_direction is BeliefDirection.NEUTRAL
    assert result.agreement_count == 0
    assert result.total_sources == 0
    assert result.strength_score == pytest.approx(0.0)
    assert result.regime is MarketRegime.UNKNOWN


def test_compute_consensus_weights_strength_by_agreeing_confidence() -> None:
    snapshots = [
        make_snapshot(BeliefSource.CLAUDE, BeliefDirection.BULLISH, 0.9, MarketRegime.TRENDING),
        make_snapshot(BeliefSource.CODEX, BeliefDirection.BULLISH, 0.6, MarketRegime.TRENDING),
        make_snapshot(
            BeliefSource.TECHNICAL_ENSEMBLE,
            BeliefDirection.BEARISH,
            0.2,
            MarketRegime.UNKNOWN,
        ),
    ]

    result = compute_consensus(snapshots)

    assert result.agreed_direction is BeliefDirection.BULLISH
    assert result.agreement_count == 2
    assert result.total_sources == 3
    assert result.strength_score == pytest.approx(0.5)
    assert result.regime is MarketRegime.TRENDING
