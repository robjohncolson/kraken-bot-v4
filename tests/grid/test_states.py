from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from core.types import GridPhase, OrderSide
from grid.states import (
    GridCycleRecord,
    GridLeg,
    GridLegKind,
    GridSlot,
    GridStateInvariantError,
    GridStateTransitionError,
    apply_entry_fill,
    apply_exit_fill,
    derive_phase,
)


PAIR = "DOGE/USD"
QUANTITY = Decimal("10")
SELL_ENTRY_PRICE = Decimal("1.20")
BUY_ENTRY_PRICE = Decimal("0.80")


def _entry_leg(side: OrderSide, *, price: Decimal = Decimal("1.00")) -> GridLeg:
    return GridLeg(
        side=side,
        kind=GridLegKind.ENTRY,
        price=price,
        quantity=QUANTITY,
    )


def _exit_leg(
    side: OrderSide,
    *,
    price: Decimal = Decimal("1.10"),
    fill_price: Decimal | None = None,
) -> GridLeg:
    return GridLeg(
        side=side,
        kind=GridLegKind.EXIT,
        price=price,
        quantity=QUANTITY,
        fill_price=fill_price,
    )


def _s0_slot() -> GridSlot:
    return GridSlot(
        pair=PAIR,
        a_leg=_entry_leg(OrderSide.SELL, price=SELL_ENTRY_PRICE),
        b_leg=_entry_leg(OrderSide.BUY, price=BUY_ENTRY_PRICE),
    )


def _s1a_slot() -> GridSlot:
    slot, _ = apply_entry_fill(
        _s0_slot(),
        filled_side=OrderSide.BUY,
        fill_price=BUY_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
    )
    return slot


def _s1b_slot() -> GridSlot:
    slot, _ = apply_entry_fill(
        _s0_slot(),
        filled_side=OrderSide.SELL,
        fill_price=SELL_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
    )
    return slot


def _s2_slot() -> GridSlot:
    slot, _ = apply_entry_fill(
        _s1a_slot(),
        filled_side=OrderSide.SELL,
        fill_price=SELL_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
    )
    return slot


def test_grid_models_are_frozen() -> None:
    leg = _entry_leg(OrderSide.BUY)
    slot = GridSlot(pair=PAIR, a_leg=leg, b_leg=_entry_leg(OrderSide.SELL))
    cycle = GridCycleRecord(
        pair=PAIR,
        entry_side=OrderSide.BUY,
        quantity=QUANTITY,
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
        pair=PAIR,
        a_leg=_entry_leg(OrderSide.SELL),
        b_leg=_entry_leg(OrderSide.BUY),
    )

    assert derive_phase(slot) is GridPhase.S0


def test_derive_phase_returns_s1a_when_b_leg_is_exit() -> None:
    slot = GridSlot(
        pair=PAIR,
        a_leg=_entry_leg(OrderSide.SELL),
        b_leg=_exit_leg(OrderSide.SELL),
    )

    assert derive_phase(slot) is GridPhase.S1A


def test_derive_phase_returns_s1b_when_a_leg_is_exit() -> None:
    slot = GridSlot(
        pair=PAIR,
        a_leg=_exit_leg(OrderSide.BUY),
        b_leg=_entry_leg(OrderSide.BUY),
    )

    assert derive_phase(slot) is GridPhase.S1B


def test_derive_phase_returns_s2_when_both_legs_are_exit() -> None:
    slot = GridSlot(
        pair=PAIR,
        a_leg=_exit_leg(OrderSide.BUY),
        b_leg=_exit_leg(OrderSide.SELL),
    )

    assert derive_phase(slot) is GridPhase.S2


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        (GridSlot(pair=PAIR), GridPhase.S0),
        (
            GridSlot(pair=PAIR, a_leg=_entry_leg(OrderSide.SELL)),
            GridPhase.S0,
        ),
        (
            GridSlot(pair=PAIR, b_leg=_entry_leg(OrderSide.BUY)),
            GridPhase.S0,
        ),
        (
            GridSlot(pair=PAIR, a_leg=_exit_leg(OrderSide.BUY)),
            GridPhase.S1B,
        ),
        (
            GridSlot(pair=PAIR, b_leg=_exit_leg(OrderSide.SELL)),
            GridPhase.S1A,
        ),
    ],
)
def test_derive_phase_handles_missing_and_partial_legs(
    slot: GridSlot, expected: GridPhase
) -> None:
    assert derive_phase(slot) is expected


def test_apply_entry_fill_transitions_s0_to_s1a() -> None:
    slot = _s0_slot()

    updated, cycles = apply_entry_fill(
        slot,
        filled_side=OrderSide.BUY,
        fill_price=BUY_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
        exit_order_id="exit-b",
        exit_client_order_id="client-exit-b",
    )

    assert updated is not slot
    assert derive_phase(updated) is GridPhase.S1A
    assert updated.a_leg == slot.a_leg
    assert updated.b_leg == GridLeg(
        side=OrderSide.SELL,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        order_id="exit-b",
        client_order_id="client-exit-b",
        fill_price=BUY_ENTRY_PRICE,
    )
    assert cycles == []
    assert slot.b_leg == _entry_leg(OrderSide.BUY, price=BUY_ENTRY_PRICE)


def test_apply_entry_fill_transitions_s0_to_s1b() -> None:
    slot = _s0_slot()

    updated, cycles = apply_entry_fill(
        slot,
        filled_side=OrderSide.SELL,
        fill_price=SELL_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
    )

    assert derive_phase(updated) is GridPhase.S1B
    assert updated.a_leg == GridLeg(
        side=OrderSide.BUY,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=SELL_ENTRY_PRICE,
    )
    assert updated.b_leg == slot.b_leg
    assert cycles == []


def test_apply_entry_fill_transitions_s1a_to_s2() -> None:
    updated, cycles = apply_entry_fill(
        _s1a_slot(),
        filled_side=OrderSide.SELL,
        fill_price=SELL_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
    )

    assert derive_phase(updated) is GridPhase.S2
    assert updated.a_leg == GridLeg(
        side=OrderSide.BUY,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=SELL_ENTRY_PRICE,
    )
    assert updated.b_leg == GridLeg(
        side=OrderSide.SELL,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=BUY_ENTRY_PRICE,
    )
    assert cycles == []


def test_apply_entry_fill_transitions_s1b_to_s2() -> None:
    updated, cycles = apply_entry_fill(
        _s1b_slot(),
        filled_side=OrderSide.BUY,
        fill_price=BUY_ENTRY_PRICE,
        exit_price=Decimal("1.00"),
    )

    assert derive_phase(updated) is GridPhase.S2
    assert updated.a_leg == GridLeg(
        side=OrderSide.BUY,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=SELL_ENTRY_PRICE,
    )
    assert updated.b_leg == GridLeg(
        side=OrderSide.SELL,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=BUY_ENTRY_PRICE,
    )
    assert cycles == []


def test_apply_exit_fill_transitions_s1a_to_s0_and_records_cycle() -> None:
    updated, cycles = apply_exit_fill(
        _s1a_slot(),
        filled_side=OrderSide.SELL,
        fill_price=Decimal("1.00"),
        next_entry_price=Decimal("0.75"),
        fees_usd=Decimal("0.10"),
        next_entry_order_id="entry-b",
        next_entry_client_order_id="client-entry-b",
    )

    assert derive_phase(updated) is GridPhase.S0
    assert updated.b_leg == GridLeg(
        side=OrderSide.BUY,
        kind=GridLegKind.ENTRY,
        price=Decimal("0.75"),
        quantity=QUANTITY,
        order_id="entry-b",
        client_order_id="client-entry-b",
    )
    assert cycles == [
        GridCycleRecord(
            pair=PAIR,
            entry_side=OrderSide.BUY,
            quantity=QUANTITY,
            entry_price=BUY_ENTRY_PRICE,
            exit_price=Decimal("1.00"),
            gross_pnl_usd=Decimal("2.00"),
            fees_usd=Decimal("0.10"),
            net_pnl_usd=Decimal("1.90"),
        )
    ]


def test_apply_exit_fill_transitions_s1b_to_s0_and_records_cycle() -> None:
    updated, cycles = apply_exit_fill(
        _s1b_slot(),
        filled_side=OrderSide.BUY,
        fill_price=Decimal("1.00"),
        next_entry_price=Decimal("1.25"),
        fees_usd=Decimal("0.05"),
    )

    assert derive_phase(updated) is GridPhase.S0
    assert updated.a_leg == GridLeg(
        side=OrderSide.SELL,
        kind=GridLegKind.ENTRY,
        price=Decimal("1.25"),
        quantity=QUANTITY,
    )
    assert cycles == [
        GridCycleRecord(
            pair=PAIR,
            entry_side=OrderSide.SELL,
            quantity=QUANTITY,
            entry_price=SELL_ENTRY_PRICE,
            exit_price=Decimal("1.00"),
            gross_pnl_usd=Decimal("2.00"),
            fees_usd=Decimal("0.05"),
            net_pnl_usd=Decimal("1.95"),
        )
    ]


def test_apply_exit_fill_transitions_s2_to_s1a_when_a_exit_fills() -> None:
    updated, cycles = apply_exit_fill(
        _s2_slot(),
        filled_side=OrderSide.BUY,
        fill_price=Decimal("1.00"),
        next_entry_price=Decimal("1.25"),
    )

    assert derive_phase(updated) is GridPhase.S1A
    assert updated.a_leg == GridLeg(
        side=OrderSide.SELL,
        kind=GridLegKind.ENTRY,
        price=Decimal("1.25"),
        quantity=QUANTITY,
    )
    assert updated.b_leg == GridLeg(
        side=OrderSide.SELL,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=BUY_ENTRY_PRICE,
    )
    assert cycles == [
        GridCycleRecord(
            pair=PAIR,
            entry_side=OrderSide.SELL,
            quantity=QUANTITY,
            entry_price=SELL_ENTRY_PRICE,
            exit_price=Decimal("1.00"),
            gross_pnl_usd=Decimal("2.00"),
            fees_usd=Decimal("0"),
            net_pnl_usd=Decimal("2.00"),
        )
    ]


def test_apply_exit_fill_transitions_s2_to_s1b_when_b_exit_fills() -> None:
    updated, cycles = apply_exit_fill(
        _s2_slot(),
        filled_side=OrderSide.SELL,
        fill_price=Decimal("1.00"),
        next_entry_price=Decimal("0.75"),
    )

    assert derive_phase(updated) is GridPhase.S1B
    assert updated.a_leg == GridLeg(
        side=OrderSide.BUY,
        kind=GridLegKind.EXIT,
        price=Decimal("1.00"),
        quantity=QUANTITY,
        fill_price=SELL_ENTRY_PRICE,
    )
    assert updated.b_leg == GridLeg(
        side=OrderSide.BUY,
        kind=GridLegKind.ENTRY,
        price=Decimal("0.75"),
        quantity=QUANTITY,
    )
    assert cycles == [
        GridCycleRecord(
            pair=PAIR,
            entry_side=OrderSide.BUY,
            quantity=QUANTITY,
            entry_price=BUY_ENTRY_PRICE,
            exit_price=Decimal("1.00"),
            gross_pnl_usd=Decimal("2.00"),
            fees_usd=Decimal("0"),
            net_pnl_usd=Decimal("2.00"),
        )
    ]


def test_apply_entry_fill_rejects_invalid_transitions() -> None:
    with pytest.raises(GridStateTransitionError):
        apply_entry_fill(
            _s1a_slot(),
            filled_side=OrderSide.BUY,
            fill_price=BUY_ENTRY_PRICE,
            exit_price=Decimal("1.00"),
        )

    with pytest.raises(GridStateTransitionError):
        apply_entry_fill(
            _s2_slot(),
            filled_side=OrderSide.SELL,
            fill_price=SELL_ENTRY_PRICE,
            exit_price=Decimal("0.95"),
        )


def test_apply_exit_fill_rejects_invalid_transitions() -> None:
    with pytest.raises(GridStateTransitionError):
        apply_exit_fill(
            _s0_slot(),
            filled_side=OrderSide.SELL,
            fill_price=Decimal("1.00"),
            next_entry_price=Decimal("0.75"),
        )

    with pytest.raises(GridStateTransitionError):
        apply_exit_fill(
            _s1a_slot(),
            filled_side=OrderSide.BUY,
            fill_price=Decimal("1.00"),
            next_entry_price=Decimal("1.25"),
        )


def test_apply_exit_fill_rejects_exit_leg_without_entry_fill_price() -> None:
    slot = GridSlot(
        pair=PAIR,
        a_leg=_entry_leg(OrderSide.SELL, price=SELL_ENTRY_PRICE),
        b_leg=_exit_leg(OrderSide.SELL, price=Decimal("1.00")),
    )

    with pytest.raises(GridStateInvariantError):
        apply_exit_fill(
            slot,
            filled_side=OrderSide.SELL,
            fill_price=Decimal("1.00"),
            next_entry_price=Decimal("0.75"),
        )
