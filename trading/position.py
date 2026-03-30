from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TypeAlias

from core.config import Settings
from core.errors import KrakenBotError
from core.types import (
    Action,
    BeliefDirection,
    BeliefSnapshot,
    ClosePosition,
    Position,
    PositionSide,
    Price,
    UpdateStop,
    UpdateTarget,
    ZERO_DECIMAL,
)

PERCENT_BASE = Decimal("100")

PositionLifecycleActions: TypeAlias = tuple[Action, ...]
PositionLifecycleResult: TypeAlias = tuple[Position, PositionLifecycleActions]


class PositionLifecycleError(KrakenBotError):
    """Base exception for pure position lifecycle transitions."""


class InvalidPositionDraftError(PositionLifecycleError):
    """Raised when a draft position cannot be opened."""

    def __init__(self, position_id: str, detail: str) -> None:
        self.position_id = position_id
        self.detail = detail
        super().__init__(f"Position {position_id!r} is not a valid draft: {detail}")


class PositionAlreadyOpenError(PositionLifecycleError):
    """Raised when open_position is called on an already-open position."""

    def __init__(self, position_id: str) -> None:
        self.position_id = position_id
        super().__init__(f"Position {position_id!r} is already open.")


class PositionNotOpenError(PositionLifecycleError):
    """Raised when a transition requires an open position but receives a draft."""

    def __init__(self, position_id: str) -> None:
        self.position_id = position_id
        super().__init__(f"Position {position_id!r} is not open.")


class PositionAlreadyClosedError(PositionLifecycleError):
    """Raised when a transition is requested for a closed position."""

    def __init__(self, position_id: str) -> None:
        self.position_id = position_id
        super().__init__(f"Position {position_id!r} is already closed.")


class InvalidBeliefForPositionError(PositionLifecycleError):
    """Raised when a belief cannot open the requested position side."""

    def __init__(self, position_id: str, detail: str) -> None:
        self.position_id = position_id
        self.detail = detail
        super().__init__(f"Belief is invalid for position {position_id!r}: {detail}")


class EmptyCloseReasonError(PositionLifecycleError):
    """Raised when close_position receives a blank reason."""

    def __init__(self, position_id: str) -> None:
        self.position_id = position_id
        super().__init__(f"Close reason is required for position {position_id!r}.")


class InvalidRiskPercentageError(PositionLifecycleError):
    """Raised when a configured stop or target percentage is non-positive."""

    def __init__(self, field_name: str, raw_value: int) -> None:
        self.field_name = field_name
        self.raw_value = raw_value
        super().__init__(f"{field_name} must be positive; got {raw_value}.")


class InvalidStopPriceError(PositionLifecycleError):
    """Raised when a proposed stop is on the wrong side of entry."""

    def __init__(self, position_id: str, stop_price: Price) -> None:
        self.position_id = position_id
        self.stop_price = stop_price
        super().__init__(f"Stop price {stop_price} is invalid for position {position_id!r}.")


class InvalidTargetPriceError(PositionLifecycleError):
    """Raised when a proposed target is on the wrong side of entry."""

    def __init__(self, position_id: str, target_price: Price) -> None:
        self.position_id = position_id
        self.target_price = target_price
        super().__init__(
            f"Target price {target_price} is invalid for position {position_id!r}."
        )


class PositionLifecycle:
    """Pure state transitions for belief-position management."""

    @staticmethod
    def open_position(
        position: Position,
        *,
        belief: BeliefSnapshot,
        config: Settings,
    ) -> PositionLifecycleResult:
        _validate_draft(position)
        _validate_belief(position, belief)
        stop_price = _initial_stop_price(position.entry_price, position.side, config.default_stop_pct)
        target_price = _initial_target_price(
            position.entry_price,
            position.side,
            config.default_target_pct,
        )
        opened = replace(position, stop_price=stop_price, target_price=target_price)
        return (
            opened,
            (
                UpdateStop(position_id=position.position_id, stop_price=stop_price),
                UpdateTarget(position_id=position.position_id, target_price=target_price),
            ),
        )

    @staticmethod
    def close_position(
        position: Position,
        *,
        reason: str,
        exit_price: Decimal | None = None,
    ) -> PositionLifecycleResult:
        _require_open_position(position)
        rendered_reason = reason.strip()
        if not rendered_reason:
            raise EmptyCloseReasonError(position.position_id)
        closed = replace(
            position,
            quantity=ZERO_DECIMAL,
            stop_price=ZERO_DECIMAL,
            target_price=ZERO_DECIMAL,
        )
        return (
            closed,
            (ClosePosition(
                position_id=position.position_id,
                reason=rendered_reason,
                pair=position.pair,
                side=position.side,
                quantity=position.quantity,
                limit_price=exit_price or position.entry_price,
            ),),
        )

    @staticmethod
    def update_stop(
        position: Position,
        *,
        stop_price: Price,
    ) -> PositionLifecycleResult:
        _require_open_position(position)
        _validate_stop_price(position, stop_price)
        updated = replace(position, stop_price=stop_price)
        return updated, (UpdateStop(position_id=position.position_id, stop_price=stop_price),)

    @staticmethod
    def update_target(
        position: Position,
        *,
        target_price: Price,
    ) -> PositionLifecycleResult:
        _require_open_position(position)
        _validate_target_price(position, target_price)
        updated = replace(position, target_price=target_price)
        return (
            updated,
            (UpdateTarget(position_id=position.position_id, target_price=target_price),),
        )


def open_position(
    position: Position,
    *,
    belief: BeliefSnapshot,
    config: Settings,
) -> PositionLifecycleResult:
    return PositionLifecycle.open_position(position, belief=belief, config=config)


def close_position(
    position: Position,
    *,
    reason: str,
    exit_price: Decimal | None = None,
) -> PositionLifecycleResult:
    return PositionLifecycle.close_position(position, reason=reason, exit_price=exit_price)


def update_stop(
    position: Position,
    *,
    stop_price: Price,
) -> PositionLifecycleResult:
    return PositionLifecycle.update_stop(position, stop_price=stop_price)


def update_target(
    position: Position,
    *,
    target_price: Price,
) -> PositionLifecycleResult:
    return PositionLifecycle.update_target(position, target_price=target_price)


def _validate_draft(position: Position) -> None:
    if position.quantity == ZERO_DECIMAL:
        raise PositionAlreadyClosedError(position.position_id)
    if position.quantity < ZERO_DECIMAL:
        raise InvalidPositionDraftError(position.position_id, "quantity must be positive")
    if position.entry_price <= ZERO_DECIMAL:
        raise InvalidPositionDraftError(position.position_id, "entry_price must be positive")
    if position.stop_price != ZERO_DECIMAL or position.target_price != ZERO_DECIMAL:
        raise PositionAlreadyOpenError(position.position_id)


def _require_open_position(position: Position) -> None:
    if position.quantity == ZERO_DECIMAL:
        raise PositionAlreadyClosedError(position.position_id)
    if position.quantity < ZERO_DECIMAL or position.entry_price <= ZERO_DECIMAL:
        raise InvalidPositionDraftError(position.position_id, "entry values must be positive")
    if position.stop_price == ZERO_DECIMAL or position.target_price == ZERO_DECIMAL:
        raise PositionNotOpenError(position.position_id)


def _validate_belief(position: Position, belief: BeliefSnapshot) -> None:
    if belief.pair != position.pair:
        raise InvalidBeliefForPositionError(
            position.position_id,
            f"belief pair {belief.pair!r} does not match {position.pair!r}",
        )
    expected_side = _side_for_belief(belief.direction)
    if expected_side is None:
        raise InvalidBeliefForPositionError(
            position.position_id,
            f"belief direction {belief.direction.value!r} cannot open a position",
        )
    if expected_side != position.side:
        raise InvalidBeliefForPositionError(
            position.position_id,
            f"belief direction {belief.direction.value!r} does not match side {position.side.value!r}",
        )


def _side_for_belief(direction: BeliefDirection) -> PositionSide | None:
    if direction == BeliefDirection.BULLISH:
        return PositionSide.LONG
    if direction == BeliefDirection.BEARISH:
        return PositionSide.SHORT
    return None


def _initial_stop_price(entry_price: Price, side: PositionSide, stop_pct: int) -> Price:
    percentage = _validated_percentage("default_stop_pct", stop_pct)
    if side == PositionSide.LONG:
        return entry_price * ((PERCENT_BASE - percentage) / PERCENT_BASE)
    return entry_price * ((PERCENT_BASE + percentage) / PERCENT_BASE)


def _initial_target_price(entry_price: Price, side: PositionSide, target_pct: int) -> Price:
    percentage = _validated_percentage("default_target_pct", target_pct)
    if side == PositionSide.LONG:
        return entry_price * ((PERCENT_BASE + percentage) / PERCENT_BASE)
    return entry_price * ((PERCENT_BASE - percentage) / PERCENT_BASE)


def _validated_percentage(field_name: str, raw_value: int) -> Decimal:
    if raw_value <= 0:
        raise InvalidRiskPercentageError(field_name, raw_value)
    return Decimal(raw_value)


def _validate_stop_price(position: Position, stop_price: Price) -> None:
    if stop_price <= ZERO_DECIMAL:
        raise InvalidStopPriceError(position.position_id, stop_price)
    if position.side == PositionSide.LONG and stop_price >= position.entry_price:
        raise InvalidStopPriceError(position.position_id, stop_price)
    if position.side == PositionSide.SHORT and stop_price <= position.entry_price:
        raise InvalidStopPriceError(position.position_id, stop_price)


def _validate_target_price(position: Position, target_price: Price) -> None:
    if target_price <= ZERO_DECIMAL:
        raise InvalidTargetPriceError(position.position_id, target_price)
    if position.side == PositionSide.LONG and target_price <= position.entry_price:
        raise InvalidTargetPriceError(position.position_id, target_price)
    if position.side == PositionSide.SHORT and target_price >= position.entry_price:
        raise InvalidTargetPriceError(position.position_id, target_price)


__all__ = [
    "close_position",
    "EmptyCloseReasonError",
    "InvalidBeliefForPositionError",
    "InvalidPositionDraftError",
    "InvalidRiskPercentageError",
    "InvalidStopPriceError",
    "InvalidTargetPriceError",
    "open_position",
    "PositionAlreadyClosedError",
    "PositionAlreadyOpenError",
    "PositionLifecycle",
    "PositionLifecycleActions",
    "PositionLifecycleError",
    "PositionLifecycleResult",
    "PositionNotOpenError",
    "update_stop",
    "update_target",
]
