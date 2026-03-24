from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from core.types import GridPhase, OrderSide
from grid.states import GridCycleRecord, GridLeg, GridLegKind, GridSlot, derive_phase


def _entry_leg(side: OrderSide) -> GridLeg:
    return GridLeg(
        side=side,
        kind=GridLegKind.ENTRY,
        price=Decimal("1.00"),
        quantity=Decimal("10"),
    )


def _exit_leg(side: OrderSide) -> GridLeg:
    return GridLeg(
        side=side,
        kind=GridLegKind.EXIT,
        price=Decimal("1.10"),
        quantity=Decimal("10"),
    )


def test_grid_models_are_frozen() -> None:
    leg = _entry_leg(OrderSide.BUY)
    slot = GridSlot(pair="DOGE/USD", a_leg=leg, b_leg=_entry_leg(OrderSide.SELL))
    cycle = GridCycleRecord(
        pair="DOGE/USD",
        entry_side=OrderSide.BUY,
        quantity=Decimal("10"),
        entry_price=Decimal("1.00"),
        exit_price=Decimal("1.10"),
        gross_pnl_usd=Decimal("1.00"),
        fees_usd=Decimal("0.10"),
        net_pnl_usd=Decimal("0.90"),
    )

    with pytest.raises(FrozenInstanceError):
        leg.price = Decimal("2.00")

    with pytest.raises(FrozenInstanceError):
        slot.pair = "BTC/USD"

    with pytest.raises(FrozenInstanceError):
        cycle.net_pnl_usd = Decimal("0.80")


def test_derive_phase_returns_s0_for_entry_slot() -> None:
    slot = GridSlot(
        pair="DOGE/USD",
        a_leg=_entry_leg(OrderSide.SELL),
        b_leg=_entry_leg(OrderSide.BUY),
    )

    assert derive_phase(slot) is GridPhase.S0


def test_derive_phase_returns_s1a_when_b_leg_is_exit() -> None:
    slot = GridSlot(
        pair="DOGE/USD",
        a_leg=_entry_leg(OrderSide.SELL),
        b_leg=_exit_leg(OrderSide.SELL),
    )

    assert derive_phase(slot) is GridPhase.S1A


def test_derive_phase_returns_s1b_when_a_leg_is_exit() -> None:
    slot = GridSlot(
        pair="DOGE/USD",
        a_leg=_exit_leg(OrderSide.BUY),
        b_leg=_entry_leg(OrderSide.BUY),
    )

    assert derive_phase(slot) is GridPhase.S1B


def test_derive_phase_returns_s2_when_both_legs_are_exit() -> None:
    slot = GridSlot(
        pair="DOGE/USD",
        a_leg=_exit_leg(OrderSide.BUY),
        b_leg=_exit_leg(OrderSide.SELL),
    )

    assert derive_phase(slot) is GridPhase.S2


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        (GridSlot(pair="DOGE/USD"), GridPhase.S0),
        (
            GridSlot(pair="DOGE/USD", a_leg=_entry_leg(OrderSide.SELL)),
            GridPhase.S0,
        ),
        (
            GridSlot(pair="DOGE/USD", b_leg=_entry_leg(OrderSide.BUY)),
            GridPhase.S0,
        ),
        (
            GridSlot(pair="DOGE/USD", a_leg=_exit_leg(OrderSide.BUY)),
            GridPhase.S1B,
        ),
        (
            GridSlot(pair="DOGE/USD", b_leg=_exit_leg(OrderSide.SELL)),
            GridPhase.S1A,
        ),
    ],
)
def test_derive_phase_handles_missing_and_partial_legs(
    slot: GridSlot, expected: GridPhase
) -> None:
    assert derive_phase(slot) is expected
