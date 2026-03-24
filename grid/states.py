from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from core.types import (
    ClientOrderId,
    GridPhase,
    OrderId,
    OrderSide,
    Pair,
    Price,
    Quantity,
    UsdAmount,
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


__all__ = [
    "GridCycleRecord",
    "GridLeg",
    "GridLegKind",
    "GridSlot",
    "derive_phase",
]
