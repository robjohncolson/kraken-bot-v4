from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import fsum
from typing import Iterable, SupportsFloat


DEFAULT_MIN_SAMPLE_SIZE = 30
DEFAULT_JARQUE_BERA_LIMIT = 5.99


class NormalityGateDecision(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True, slots=True)
class NormalityMetrics:
    skewness: float
    excess_kurtosis: float
    jarque_bera: float


@dataclass(frozen=True, slots=True)
class NormalityGateResult:
    decision: NormalityGateDecision
    reason: str
    sample_size: int
    metrics: NormalityMetrics | None = None

    @property
    def passed(self) -> bool:
        return self.decision == NormalityGateDecision.PASS

    @property
    def use_parametric(self) -> bool:
        return self.decision != NormalityGateDecision.FAIL_CLOSED

    @property
    def reduced_confidence(self) -> bool:
        return self.decision == NormalityGateDecision.WARN


def normality_gate(
    observations: Iterable[SupportsFloat],
    *,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    jarque_bera_limit: float = DEFAULT_JARQUE_BERA_LIMIT,
) -> NormalityGateResult:
    sample = tuple(float(value) for value in observations)
    sample_size = len(sample)

    if sample_size < min_sample_size:
        return NormalityGateResult(
            decision=NormalityGateDecision.FAIL_CLOSED,
            reason="insufficient_sample_size",
            sample_size=sample_size,
        )

    mean = fsum(sample) / sample_size
    centered = tuple(value - mean for value in sample)
    second_moment = fsum(value * value for value in centered) / sample_size

    if second_moment == 0.0:
        return NormalityGateResult(
            decision=NormalityGateDecision.FAIL_CLOSED,
            reason="zero_variance",
            sample_size=sample_size,
        )

    third_moment = fsum(value * value * value for value in centered) / sample_size
    fourth_moment = fsum(value * value * value * value for value in centered) / sample_size

    skewness = third_moment / (second_moment**1.5)
    excess_kurtosis = (fourth_moment / (second_moment**2)) - 3.0
    jarque_bera = (sample_size / 6.0) * (
        (skewness * skewness) + ((excess_kurtosis * excess_kurtosis) / 4.0)
    )

    metrics = NormalityMetrics(
        skewness=skewness,
        excess_kurtosis=excess_kurtosis,
        jarque_bera=jarque_bera,
    )

    if jarque_bera <= jarque_bera_limit:
        return NormalityGateResult(
            decision=NormalityGateDecision.PASS,
            reason="jarque_bera_within_limit",
            sample_size=sample_size,
            metrics=metrics,
        )

    return NormalityGateResult(
        decision=NormalityGateDecision.WARN,
        reason="jarque_bera_above_limit",
        sample_size=sample_size,
        metrics=metrics,
    )


__all__ = [
    "DEFAULT_JARQUE_BERA_LIMIT",
    "DEFAULT_MIN_SAMPLE_SIZE",
    "NormalityGateDecision",
    "NormalityGateResult",
    "NormalityMetrics",
    "normality_gate",
]
