from __future__ import annotations

import pytest

from stats.normality import NormalityGateDecision, normality_gate


PASS_SAMPLE = (
    -2.1,
    -1.7,
    -1.5,
    -1.3,
    -1.2,
    -1.0,
    -0.9,
    -0.8,
    -0.7,
    -0.6,
    -0.5,
    -0.4,
    -0.3,
    -0.2,
    -0.1,
    0.1,
    0.2,
    0.3,
    0.4,
    0.5,
    0.6,
    0.7,
    0.8,
    0.9,
    1.0,
    1.2,
    1.3,
    1.5,
    1.7,
    2.1,
)

FAIL_SAMPLE = (
    0.1,
    0.1,
    0.2,
    0.2,
    0.2,
    0.3,
    0.3,
    0.3,
    0.4,
    0.4,
    0.4,
    0.5,
    0.5,
    0.5,
    0.5,
    0.6,
    0.6,
    0.6,
    0.7,
    0.7,
    0.8,
    0.8,
    0.9,
    1.0,
    1.1,
    1.2,
    1.4,
    2.5,
    3.5,
    4.8,
)


def test_normality_gate_passes_balanced_sample() -> None:
    result = normality_gate(PASS_SAMPLE)

    assert result.decision == NormalityGateDecision.PASS
    assert result.reason == "jarque_bera_within_limit"
    assert result.sample_size == 30
    assert result.passed is True
    assert result.use_parametric is True
    assert result.reduced_confidence is False
    assert result.metrics is not None
    assert result.metrics.skewness == pytest.approx(0.0)
    assert result.metrics.excess_kurtosis == pytest.approx(-0.7862556447442532)
    assert result.metrics.jarque_bera == pytest.approx(0.7727474236152516)


def test_normality_gate_warns_for_skewed_sample() -> None:
    result = normality_gate(FAIL_SAMPLE)

    assert result.decision == NormalityGateDecision.WARN
    assert result.reason == "jarque_bera_above_limit"
    assert result.sample_size == 30
    assert result.passed is False
    assert result.use_parametric is True
    assert result.reduced_confidence is True
    assert result.metrics is not None
    assert result.metrics.skewness == pytest.approx(2.6186809874801327)
    assert result.metrics.excess_kurtosis == pytest.approx(6.537251713798938)
    assert result.metrics.jarque_bera == pytest.approx(87.70702553290855)


def test_normality_gate_fails_closed_for_small_samples() -> None:
    result = normality_gate((-1.0, -0.5, 0.0, 0.5, 1.0))

    assert result.decision == NormalityGateDecision.FAIL_CLOSED
    assert result.reason == "insufficient_sample_size"
    assert result.sample_size == 5
    assert result.passed is False
    assert result.use_parametric is False
    assert result.reduced_confidence is False
    assert result.metrics is None


def test_normality_gate_fails_closed_for_zero_variance() -> None:
    result = normality_gate((1.0,) * 30)

    assert result.decision == NormalityGateDecision.FAIL_CLOSED
    assert result.reason == "zero_variance"
    assert result.sample_size == 30
    assert result.passed is False
    assert result.use_parametric is False
    assert result.reduced_confidence is False
    assert result.metrics is None
