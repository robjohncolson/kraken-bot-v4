from __future__ import annotations

from decimal import Decimal

from trading.sizing import bounded_kelly, kelly_fraction, size_position_usd


def test_kelly_fraction_uses_win_probability_and_payoff_ratio() -> None:
    result = kelly_fraction(win_probability=Decimal("0.60"), payoff_ratio=Decimal("2"))

    assert result == Decimal("0.40")


def test_kelly_fraction_returns_zero_for_edge_cases_without_positive_edge() -> None:
    assert kelly_fraction(win_probability=Decimal("0.00"), payoff_ratio=Decimal("2")) == Decimal("0")
    assert kelly_fraction(win_probability=Decimal("1.00"), payoff_ratio=Decimal("2")) == Decimal("1")
    assert kelly_fraction(win_probability=Decimal("0.55"), payoff_ratio=Decimal("0")) == Decimal("0")


def test_bounded_kelly_uses_lower_confidence_bound() -> None:
    point_estimate = kelly_fraction(win_probability=Decimal("0.90"), payoff_ratio=Decimal("2"))

    bounded_95 = bounded_kelly(wins=18, losses=2, payoff_ratio=Decimal("2"))
    bounded_90 = bounded_kelly(
        wins=18,
        losses=2,
        payoff_ratio=Decimal("2"),
        confidence_level=Decimal("0.90"),
    )

    assert bounded_95 > Decimal("0")
    assert bounded_95 < point_estimate
    assert bounded_90 > bounded_95


def test_size_position_usd_clamps_to_minimum_and_maximum_bounds() -> None:
    assert (
        size_position_usd(
            portfolio_value_usd=Decimal("100"),
            kelly_fraction_value=Decimal("0.05"),
            min_position_usd=Decimal("10"),
            max_position_usd=Decimal("100"),
        )
        == Decimal("10")
    )
    assert (
        size_position_usd(
            portfolio_value_usd=Decimal("1000"),
            kelly_fraction_value=Decimal("0.50"),
            min_position_usd=Decimal("10"),
            max_position_usd=Decimal("100"),
        )
        == Decimal("100")
    )


def test_size_position_usd_returns_zero_when_no_position_should_be_taken() -> None:
    assert (
        size_position_usd(
            portfolio_value_usd=Decimal("100"),
            kelly_fraction_value=Decimal("0"),
            min_position_usd=Decimal("10"),
            max_position_usd=Decimal("100"),
        )
        == Decimal("0")
    )
