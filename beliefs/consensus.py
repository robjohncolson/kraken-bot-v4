from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from core.types import BeliefDirection, BeliefSnapshot, MarketRegime

CONSENSUS_RATIO = 2 / 3


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    agreed_direction: BeliefDirection
    agreement_count: int
    total_sources: int
    strength_score: float
    regime: MarketRegime


def compute_consensus(snapshots: Sequence[BeliefSnapshot]) -> ConsensusResult:
    total_sources = len(snapshots)
    if total_sources == 0:
        return ConsensusResult(
            agreed_direction=BeliefDirection.NEUTRAL,
            agreement_count=0,
            total_sources=0,
            strength_score=0.0,
            regime=MarketRegime.UNKNOWN,
        )

    direction_counts = Counter(snapshot.direction for snapshot in snapshots)
    agreement_count = max(direction_counts.values(), default=0)
    required_votes = math.ceil(total_sources * CONSENSUS_RATIO)
    winners = [
        direction for direction, count in direction_counts.items() if count == agreement_count
    ]
    agreed_direction = (
        winners[0]
        if agreement_count >= required_votes and len(winners) == 1
        else BeliefDirection.NEUTRAL
    )

    if agreed_direction is BeliefDirection.NEUTRAL and len(winners) != 1:
        strength_score = 0.0
    elif agreed_direction is BeliefDirection.NEUTRAL and agreement_count < required_votes:
        strength_score = 0.0
    else:
        agreeing_confidence = sum(
            snapshot.confidence
            for snapshot in snapshots
            if snapshot.direction is agreed_direction
        )
        strength_score = round(agreeing_confidence / total_sources, 2)

    return ConsensusResult(
        agreed_direction=agreed_direction,
        agreement_count=agreement_count,
        total_sources=total_sources,
        strength_score=strength_score,
        regime=_majority_regime(snapshots),
    )


def _majority_regime(snapshots: Sequence[BeliefSnapshot]) -> MarketRegime:
    reported_regimes = [
        snapshot.regime
        for snapshot in snapshots
        if snapshot.regime is not MarketRegime.UNKNOWN
    ]
    if not reported_regimes:
        return MarketRegime.UNKNOWN

    regime_counts = Counter(reported_regimes)
    majority_count = max(regime_counts.values(), default=0)
    winners = [regime for regime, count in regime_counts.items() if count == majority_count]

    if len(winners) != 1 or majority_count <= len(reported_regimes) / 2:
        return MarketRegime.UNKNOWN

    return winners[0]


__all__ = ["ConsensusResult", "compute_consensus"]
