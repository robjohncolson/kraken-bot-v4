from __future__ import annotations

from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from core.config import (
    DEFAULT_KELLY_CI_LEVEL,
    DEFAULT_MAX_POSITION_USD,
    DEFAULT_MIN_POSITION_USD,
)
from core.errors import KrakenBotError
from core.types import UsdAmount, ZERO_DECIMAL

ONE_DECIMAL = Decimal("1")
DEFAULT_CONFIDENCE_LEVEL = Decimal(str(DEFAULT_KELLY_CI_LEVEL))
DEFAULT_MIN_USD = Decimal(str(DEFAULT_MIN_POSITION_USD))
DEFAULT_MAX_USD = Decimal(str(DEFAULT_MAX_POSITION_USD))

CONFIDENCE_Z_SCORES: Mapping[Decimal, Decimal] = MappingProxyType(
    {
        Decimal("0.80"): Decimal("1.2815515655446004"),
        Decimal("0.90"): Decimal("1.6448536269514722"),
        Decimal("0.95"): Decimal("1.959963984540054"),
        Decimal("0.98"): Decimal("2.3263478740408408"),
        Decimal("0.99"): Decimal("2.5758293035489004"),
    }
)


class PositionSizingError(KrakenBotError):
    """Base exception for position sizing failures."""


class InvalidWinProbabilityError(PositionSizingError):
    """Raised when Kelly sizing receives an invalid win probability."""

    def __init__(self, win_probability: Decimal) -> None:
        self.win_probability = win_probability
        super().__init__(f"win_probability must be between 0 and 1; got {win_probability}.")


class InvalidPayoffRatioError(PositionSizingError):
    """Raised when Kelly sizing receives a negative payoff ratio."""

    def __init__(self, payoff_ratio: Decimal) -> None:
        self.payoff_ratio = payoff_ratio
        super().__init__(f"payoff_ratio must be non-negative; got {payoff_ratio}.")


class InvalidTradeCountError(PositionSizingError):
    """Raised when bounded Kelly receives a negative trade count."""

    def __init__(self, label: str, value: int) -> None:
        self.label = label
        self.value = value
        super().__init__(f"{label} must be non-negative; got {value}.")


class InvalidConfidenceLevelError(PositionSizingError):
    """Raised when the requested confidence level is unsupported."""

    def __init__(self, confidence_level: Decimal) -> None:
        self.confidence_level = confidence_level
        supported = ", ".join(str(level) for level in sorted(CONFIDENCE_Z_SCORES))
        super().__init__(
            f"confidence_level {confidence_level} is unsupported; expected one of: {supported}."
        )


class InvalidPositionBoundsError(PositionSizingError):
    """Raised when the configured position bounds are invalid."""

    def __init__(self, min_position_usd: UsdAmount, max_position_usd: UsdAmount) -> None:
        self.min_position_usd = min_position_usd
        self.max_position_usd = max_position_usd
        super().__init__(
            "Position bounds must be non-negative and max_position_usd must be >= "
            f"min_position_usd; got min={min_position_usd}, max={max_position_usd}."
        )


class InvalidPortfolioValueError(PositionSizingError):
    """Raised when size_position_usd receives a negative portfolio value."""

    def __init__(self, portfolio_value_usd: UsdAmount) -> None:
        self.portfolio_value_usd = portfolio_value_usd
        super().__init__(f"portfolio_value_usd must be non-negative; got {portfolio_value_usd}.")


def kelly_fraction(win_probability: Decimal, payoff_ratio: Decimal) -> Decimal:
    """Return the non-negative Kelly fraction for a win rate and payoff ratio."""

    validated_probability = _validate_win_probability(win_probability)
    validated_payoff = _validate_payoff_ratio(payoff_ratio)
    if validated_payoff == ZERO_DECIMAL:
        return ZERO_DECIMAL

    loss_probability = ONE_DECIMAL - validated_probability
    fraction = validated_probability - (loss_probability / validated_payoff)
    return max(fraction, ZERO_DECIMAL)


def bounded_kelly(
    wins: int,
    losses: int,
    payoff_ratio: Decimal,
    confidence_level: Decimal = DEFAULT_CONFIDENCE_LEVEL,
) -> Decimal:
    """Return Kelly sizing using the lower bound of a win-rate confidence interval."""

    _validate_trade_count("wins", wins)
    _validate_trade_count("losses", losses)
    point_estimate = _win_probability(wins, losses)
    lower_bound = _win_probability_lower_bound(wins, losses, confidence_level)
    return min(
        kelly_fraction(point_estimate, payoff_ratio),
        kelly_fraction(lower_bound, payoff_ratio),
    )


def size_position_usd(
    portfolio_value_usd: UsdAmount,
    kelly_fraction_value: Decimal,
    min_position_usd: UsdAmount = DEFAULT_MIN_USD,
    max_position_usd: UsdAmount = DEFAULT_MAX_USD,
) -> UsdAmount:
    """Convert a Kelly fraction into a bounded USD allocation."""

    _validate_position_bounds(min_position_usd, max_position_usd)
    if portfolio_value_usd < ZERO_DECIMAL:
        raise InvalidPortfolioValueError(portfolio_value_usd)
    if portfolio_value_usd == ZERO_DECIMAL or kelly_fraction_value <= ZERO_DECIMAL:
        return ZERO_DECIMAL
    if portfolio_value_usd < min_position_usd:
        return ZERO_DECIMAL

    raw_size = portfolio_value_usd * kelly_fraction_value
    capped_maximum = min(max_position_usd, portfolio_value_usd)
    if raw_size < min_position_usd:
        return min_position_usd
    return min(raw_size, capped_maximum)


def _win_probability(wins: int, losses: int) -> Decimal:
    total_trades = wins + losses
    if total_trades == 0:
        return ZERO_DECIMAL
    return Decimal(wins) / Decimal(total_trades)


def _win_probability_lower_bound(
    wins: int,
    losses: int,
    confidence_level: Decimal,
) -> Decimal:
    total_trades = wins + losses
    if total_trades == 0:
        return ZERO_DECIMAL

    win_probability = _win_probability(wins, losses)
    z_score = _z_score_for(confidence_level)
    variance = (win_probability * (ONE_DECIMAL - win_probability)) / Decimal(total_trades)
    margin_of_error = z_score * variance.sqrt()
    return max(win_probability - margin_of_error, ZERO_DECIMAL)


def _validate_win_probability(win_probability: Decimal) -> Decimal:
    if win_probability < ZERO_DECIMAL or win_probability > ONE_DECIMAL:
        raise InvalidWinProbabilityError(win_probability)
    return win_probability


def _validate_payoff_ratio(payoff_ratio: Decimal) -> Decimal:
    if payoff_ratio < ZERO_DECIMAL:
        raise InvalidPayoffRatioError(payoff_ratio)
    return payoff_ratio


def _validate_trade_count(label: str, value: int) -> None:
    if value < 0:
        raise InvalidTradeCountError(label, value)


def _validate_position_bounds(min_position_usd: UsdAmount, max_position_usd: UsdAmount) -> None:
    if (
        min_position_usd < ZERO_DECIMAL
        or max_position_usd < ZERO_DECIMAL
        or max_position_usd < min_position_usd
    ):
        raise InvalidPositionBoundsError(min_position_usd, max_position_usd)


def _z_score_for(confidence_level: Decimal) -> Decimal:
    normalized_level = Decimal(str(confidence_level))
    try:
        return CONFIDENCE_Z_SCORES[normalized_level]
    except KeyError as exc:
        raise InvalidConfidenceLevelError(normalized_level) from exc


__all__ = [
    "bounded_kelly",
    "CONFIDENCE_Z_SCORES",
    "DEFAULT_CONFIDENCE_LEVEL",
    "DEFAULT_MAX_USD",
    "DEFAULT_MIN_USD",
    "InvalidConfidenceLevelError",
    "InvalidPayoffRatioError",
    "InvalidPortfolioValueError",
    "InvalidPositionBoundsError",
    "InvalidTradeCountError",
    "InvalidWinProbabilityError",
    "kelly_fraction",
    "PositionSizingError",
    "size_position_usd",
]
