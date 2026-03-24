from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from core.types import (
    ClientOrderId,
    GridPhase,
    OrderId,
    OrderSide,
    Pair,
    Price,
    Quantity,
    UsdAmount,
    ZERO_DECIMAL,
)


class GridLegKind(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"


@dataclass(frozen=True, slots=True)
class GridLeg:
    """One active managed order inside a grid slot."""

    side: OrderSide
    kind: GridLegKind
    price: Price
    quantity: Quantity
    order_id: OrderId | None = None
    client_order_id: ClientOrderId | None = None
    fill_price: Price | None = None


@dataclass(frozen=True, slots=True)
class GridSlot:
    """One grid position containing the upper A branch and lower B branch."""

    pair: Pair
    a_leg: GridLeg | None = None
    b_leg: GridLeg | None = None


@dataclass(frozen=True, slots=True)
class GridCycleRecord:
    """A completed entry/exit round trip for one grid leg."""

    pair: Pair
    entry_side: OrderSide
    quantity: Quantity
    entry_price: Price
    exit_price: Price
    gross_pnl_usd: UsdAmount
    fees_usd: UsdAmount
    net_pnl_usd: UsdAmount


class GridStateTransitionError(ValueError):
    """Raised when a fill does not match the slot's current lifecycle state."""


class GridStateInvariantError(GridStateTransitionError):
    """Raised when a slot carries inconsistent state for a valid transition."""


def derive_phase(slot: GridSlot) -> GridPhase:
    """Derive the V2-style grid phase from the slot's currently active legs."""

    a_kind = _leg_kind(slot.a_leg)
    b_kind = _leg_kind(slot.b_leg)

    if a_kind is GridLegKind.EXIT and b_kind is GridLegKind.EXIT:
        return GridPhase.S2
    if b_kind is GridLegKind.EXIT:
        return GridPhase.S1A
    if a_kind is GridLegKind.EXIT:
        return GridPhase.S1B
    return GridPhase.S0


def _leg_kind(leg: GridLeg | None) -> GridLegKind | None:
    if leg is None:
        return None
    return leg.kind


def apply_entry_fill(
    slot: GridSlot,
    *,
    filled_side: OrderSide,
    fill_price: Price,
    exit_price: Price,
    exit_order_id: OrderId | None = None,
    exit_client_order_id: ClientOrderId | None = None,
) -> tuple[GridSlot, list[GridCycleRecord]]:
    """Apply a fully filled entry order and create its matching exit leg."""

    leg_name, entry_leg = _find_leg(slot, kind=GridLegKind.ENTRY, side=filled_side)
    updated_leg = GridLeg(
        side=_opposite_side(entry_leg.side),
        kind=GridLegKind.EXIT,
        price=exit_price,
        quantity=entry_leg.quantity,
        order_id=exit_order_id,
        client_order_id=exit_client_order_id,
        fill_price=fill_price,
    )
    return _replace_leg(slot, leg_name, updated_leg), []


def apply_exit_fill(
    slot: GridSlot,
    *,
    filled_side: OrderSide,
    fill_price: Price,
    next_entry_price: Price,
    fees_usd: UsdAmount = ZERO_DECIMAL,
    next_entry_order_id: OrderId | None = None,
    next_entry_client_order_id: ClientOrderId | None = None,
) -> tuple[GridSlot, list[GridCycleRecord]]:
    """Apply a fully filled exit order, book its completed cycle, and recreate the entry leg."""

    leg_name, exit_leg = _find_leg(slot, kind=GridLegKind.EXIT, side=filled_side)
    if exit_leg.fill_price is None:
        raise GridStateInvariantError(
            f"exit leg {leg_name} is missing the originating entry fill price"
        )

    entry_side = _opposite_side(exit_leg.side)
    gross_pnl = _calculate_gross_pnl(
        entry_side=entry_side,
        entry_price=exit_leg.fill_price,
        exit_price=fill_price,
        quantity=exit_leg.quantity,
    )
    cycle = GridCycleRecord(
        pair=slot.pair,
        entry_side=entry_side,
        quantity=exit_leg.quantity,
        entry_price=exit_leg.fill_price,
        exit_price=fill_price,
        gross_pnl_usd=gross_pnl,
        fees_usd=fees_usd,
        net_pnl_usd=gross_pnl - fees_usd,
    )
    reset_leg = GridLeg(
        side=entry_side,
        kind=GridLegKind.ENTRY,
        price=next_entry_price,
        quantity=exit_leg.quantity,
        order_id=next_entry_order_id,
        client_order_id=next_entry_client_order_id,
    )
    return _replace_leg(slot, leg_name, reset_leg), [cycle]


def _replace_leg(
    slot: GridSlot, leg_name: Literal["a_leg", "b_leg"], leg: GridLeg
) -> GridSlot:
    return replace(slot, **{leg_name: leg})


def _find_leg(
    slot: GridSlot,
    *,
    kind: GridLegKind,
    side: OrderSide,
) -> tuple[Literal["a_leg", "b_leg"], GridLeg]:
    matches: list[tuple[Literal["a_leg", "b_leg"], GridLeg]] = []
    for leg_name in ("a_leg", "b_leg"):
        leg = getattr(slot, leg_name)
        if leg is not None and leg.kind is kind and leg.side is side:
            matches.append((leg_name, leg))

    if not matches:
        raise GridStateTransitionError(
            f"no {kind.value} leg with side {side.value} exists for slot {slot.pair}"
        )
    if len(matches) > 1:
        raise GridStateInvariantError(
            f"multiple {kind.value} legs with side {side.value} exist for slot {slot.pair}"
        )
    return matches[0]


def _opposite_side(side: OrderSide) -> OrderSide:
    if side is OrderSide.BUY:
        return OrderSide.SELL
    return OrderSide.BUY


def _calculate_gross_pnl(
    *,
    entry_side: OrderSide,
    entry_price: Price,
    exit_price: Price,
    quantity: Quantity,
) -> UsdAmount:
    if entry_side is OrderSide.BUY:
        return (exit_price - entry_price) * quantity
    return (entry_price - exit_price) * quantity


__all__ = [
    "apply_entry_fill",
    "apply_exit_fill",
    "GridCycleRecord",
    "GridLeg",
    "GridLegKind",
    "GridStateInvariantError",
    "GridSlot",
    "GridStateTransitionError",
    "derive_phase",
]
