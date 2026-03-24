from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias

from core.config import Settings
from core.errors import KrakenBotError
from core.types import Pair, Portfolio, Position, PositionSide, Price, ZERO_DECIMAL
from trading.risk_rules import RiskCheckResult, check_portfolio_rules


class GuardianError(KrakenBotError):
    """Base exception for guardian monitoring errors."""


class MissingCurrentPriceError(GuardianError):
    """Raised when a monitored position has no current market price."""

    def __init__(self, pair: Pair) -> None:
        self.pair = pair
        super().__init__(f"Missing current price for pair {pair!r}.")


class InvalidPriceInputError(GuardianError):
    """Raised when a current price entry cannot be interpreted."""

    def __init__(self, pair: Pair, raw_value: object) -> None:
        self.pair = pair
        self.raw_value = raw_value
        super().__init__(
            f"Current price for pair {pair!r} must be a Decimal or PriceSnapshot; "
            f"got {type(raw_value).__name__}."
        )


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    price: Price
    belief_timestamp: datetime | None = None


PriceInput: TypeAlias = Price | PriceSnapshot
CurrentPrices: TypeAlias = Mapping[Pair, PriceInput]


class GuardianActionType(StrEnum):
    LIMIT_EXIT_ATTEMPT = "limit_exit_attempt"
    STOP_TRIGGERED = "stop_triggered"
    TARGET_HIT = "target_hit"
    BELIEF_STALE = "belief_stale"
    RISK_VIOLATION = "risk_violation"


@dataclass(frozen=True, slots=True)
class GuardianAction:
    action_type: GuardianActionType
    details: Mapping[str, object]


class Guardian:
    """Autonomous position monitor for exits, portfolio risk, and stale beliefs."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or _utcnow

    def check_positions(
        self,
        current_prices: CurrentPrices,
        portfolio: Portfolio,
        config: Settings,
        *,
        as_of: datetime | None = None,
    ) -> list[GuardianAction]:
        reference_time = _normalize_timestamp(as_of or self._clock())
        actions: list[GuardianAction] = []

        for position in portfolio.positions:
            snapshot = _price_snapshot(current_prices, position.pair)
            actions.extend(_monitor_price_levels(position, snapshot.price))
            if _belief_is_stale(snapshot.belief_timestamp, reference_time, config):
                actions.append(
                    GuardianAction(
                        action_type=GuardianActionType.BELIEF_STALE,
                        details={
                            "position_id": position.position_id,
                            "pair": position.pair,
                            "belief_timestamp": _normalize_timestamp(snapshot.belief_timestamp),
                            "checked_at": reference_time,
                            "stale_after_hours": config.belief_stale_hours,
                        },
                    )
                )

        risk_result = check_portfolio_rules(portfolio, config=config)
        actions.extend(_risk_actions(risk_result))
        return actions


def check_positions(
    current_prices: CurrentPrices,
    portfolio: Portfolio,
    config: Settings,
    *,
    as_of: datetime | None = None,
) -> list[GuardianAction]:
    return Guardian().check_positions(
        current_prices,
        portfolio,
        config,
        as_of=as_of,
    )


def _monitor_price_levels(position: Position, current_price: Price) -> list[GuardianAction]:
    if _stop_reached(position, current_price):
        return [
            GuardianAction(
                action_type=GuardianActionType.STOP_TRIGGERED,
                details={
                    "position_id": position.position_id,
                    "pair": position.pair,
                    "trigger_price": current_price,
                    "stop_price": position.stop_price,
                },
            ),
            GuardianAction(
                action_type=GuardianActionType.LIMIT_EXIT_ATTEMPT,
                details={
                    "position_id": position.position_id,
                    "pair": position.pair,
                    "trigger_price": current_price,
                    "reason": GuardianActionType.STOP_TRIGGERED.value,
                },
            ),
        ]

    if _target_reached(position, current_price):
        return [
            GuardianAction(
                action_type=GuardianActionType.TARGET_HIT,
                details={
                    "position_id": position.position_id,
                    "pair": position.pair,
                    "trigger_price": current_price,
                    "target_price": position.target_price,
                },
            ),
            GuardianAction(
                action_type=GuardianActionType.LIMIT_EXIT_ATTEMPT,
                details={
                    "position_id": position.position_id,
                    "pair": position.pair,
                    "trigger_price": current_price,
                    "reason": GuardianActionType.TARGET_HIT.value,
                },
            ),
        ]

    return []


def _stop_reached(position: Position, current_price: Price) -> bool:
    if position.stop_price <= ZERO_DECIMAL:
        return False
    if position.side == PositionSide.LONG:
        return current_price <= position.stop_price
    return current_price >= position.stop_price


def _target_reached(position: Position, current_price: Price) -> bool:
    if position.target_price <= ZERO_DECIMAL:
        return False
    if position.side == PositionSide.LONG:
        return current_price >= position.target_price
    return current_price <= position.target_price


def _belief_is_stale(
    belief_timestamp: datetime | None,
    reference_time: datetime,
    config: Settings,
) -> bool:
    if belief_timestamp is None:
        return False
    stale_after = timedelta(hours=config.belief_stale_hours)
    return reference_time - _normalize_timestamp(belief_timestamp) >= stale_after


def _risk_actions(risk_result: RiskCheckResult) -> list[GuardianAction]:
    if risk_result.passed:
        return []

    return [
        GuardianAction(
            action_type=GuardianActionType.RISK_VIOLATION,
            details={
                "violation": violation,
                "violation_type": type(violation).__name__,
                "recommended_action": risk_result.recommended_action,
            },
        )
        for violation in risk_result.violations
    ]


def _price_snapshot(current_prices: CurrentPrices, pair: Pair) -> PriceSnapshot:
    raw_value = current_prices.get(pair)
    if raw_value is None:
        raise MissingCurrentPriceError(pair)
    if isinstance(raw_value, PriceSnapshot):
        return raw_value
    if isinstance(raw_value, Decimal):
        return PriceSnapshot(price=raw_value)
    raise InvalidPriceInputError(pair, raw_value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_timestamp(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


__all__ = [
    "check_positions",
    "CurrentPrices",
    "Guardian",
    "GuardianAction",
    "GuardianActionType",
    "GuardianError",
    "InvalidPriceInputError",
    "MissingCurrentPriceError",
    "PriceInput",
    "PriceSnapshot",
]
