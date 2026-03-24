from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.types import (
    ActionType,
    DeactivateGrid,
    LogEvent,
    OrderRequest,
    OrderSide,
    OrderType,
    PlaceOrder,
    RedistributeGridProfits,
)
from grid.engine import (
    GridEngineState,
    GridPricePlan,
    ManagedGridSlot,
    OrphanedGridLeg,
    activate_grid,
    deactivate_grid,
    detect_orphans,
    headroom_budget,
    redistribute_profits,
)
from grid.states import GridLeg, GridLegKind, GridSlot


NOW = datetime(2026, 3, 24, 12, 0, tzinfo=UTC)
PAIR = "DOGE/USD"


def _price_plan() -> GridPricePlan:
    return GridPricePlan(
        upper_entry_price=Decimal("0.22"),
        lower_entry_price=Decimal("0.18"),
        upper_exit_price=Decimal("0.20"),
        lower_exit_price=Decimal("0.20"),
    )


def _active_state() -> GridEngineState:
    state, _ = activate_grid(
        GridEngineState(pair=PAIR),
        available_capital_usd=Decimal("35.00"),
        reference_price=Decimal("0.20"),
        price_plan=_price_plan(),
        remaining_order_capacity=10,
        grid_headroom_pct=100,
        now=NOW,
    )
    return state


def test_activate_grid_creates_initial_slots_from_sizing_and_emits_entry_orders() -> None:
    updated, actions = activate_grid(
        GridEngineState(pair=PAIR),
        available_capital_usd=Decimal("35.00"),
        reference_price=Decimal("0.20"),
        price_plan=_price_plan(),
        remaining_order_capacity=5,
        grid_headroom_pct=100,
        now=NOW,
    )

    assert updated.accepting_new_entries is True
    assert updated.allocation is not None
    assert updated.allocation.slot_count == 2
    assert updated.allocation.allocated_capital_usd == Decimal("20.00")
    assert updated.allocation.remainder_usd == Decimal("15.00")
    assert len(updated.slots) == 2
    assert updated.slots[0] == ManagedGridSlot(
        slot=GridSlot(
            pair=PAIR,
            a_leg=GridLeg(
                side=OrderSide.SELL,
                kind=GridLegKind.ENTRY,
                price=Decimal("0.22"),
                quantity=Decimal("50"),
                client_order_id="grid:doge-usd:0:a",
            ),
            b_leg=GridLeg(
                side=OrderSide.BUY,
                kind=GridLegKind.ENTRY,
                price=Decimal("0.18"),
                quantity=Decimal("50"),
                client_order_id="grid:doge-usd:0:b",
            ),
        ),
        a_opened_at=NOW,
        b_opened_at=NOW,
    )
    assert actions == (
        PlaceOrder(
            order=OrderRequest(
                pair=PAIR,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=Decimal("50"),
                limit_price=Decimal("0.22"),
                client_order_id="grid:doge-usd:0:a",
            )
        ),
        PlaceOrder(
            order=OrderRequest(
                pair=PAIR,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("50"),
                limit_price=Decimal("0.18"),
                client_order_id="grid:doge-usd:0:b",
            )
        ),
        PlaceOrder(
            order=OrderRequest(
                pair=PAIR,
                side=OrderSide.SELL,
                order_type=OrderType.LIMIT,
                quantity=Decimal("50"),
                limit_price=Decimal("0.22"),
                client_order_id="grid:doge-usd:1:a",
            )
        ),
        PlaceOrder(
            order=OrderRequest(
                pair=PAIR,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("50"),
                limit_price=Decimal("0.18"),
                client_order_id="grid:doge-usd:1:b",
            )
        ),
    )


def test_deactivate_grid_stops_new_entries_and_leaves_existing_orders_working() -> None:
    updated, actions = deactivate_grid(_active_state())

    assert updated.accepting_new_entries is False
    assert updated.slots == _active_state().slots
    assert actions == (
        DeactivateGrid(pair=PAIR),
        LogEvent(message=f"Grid deactivated for {PAIR}; existing working orders remain live."),
    )
    assert all(action.kind is not ActionType.CANCEL_ORDER for action in actions)


def test_redistribute_profits_compounds_evenly_across_active_slots() -> None:
    state = GridEngineState(
        pair=PAIR,
        slots=_active_state().slots,
        accepting_new_entries=True,
        pending_profit_usd=Decimal("4.00"),
    )
    original_slots = state.slots
    total_profit = state.pending_profit_usd + Decimal("16.00")
    expected_quantity_increment = (
        total_profit / Decimal(len(original_slots))
    ) / Decimal("0.20")

    updated, actions = redistribute_profits(
        state,
        realized_profit_usd=Decimal("16.00"),
        reference_price=Decimal("0.20"),
    )

    assert updated.pending_profit_usd == Decimal("0")
    assert updated.slots[0].slot.a_leg is not None
    assert updated.slots[0].slot.b_leg is not None
    assert updated.slots[0].slot.a_leg.quantity == (
        original_slots[0].slot.a_leg.quantity + expected_quantity_increment
    )
    assert updated.slots[0].slot.b_leg.quantity == (
        original_slots[0].slot.b_leg.quantity + expected_quantity_increment
    )
    assert updated.slots[1].slot.a_leg.quantity == (
        original_slots[1].slot.a_leg.quantity + expected_quantity_increment
    )
    assert updated.slots[1].slot.b_leg.quantity == (
        original_slots[1].slot.b_leg.quantity + expected_quantity_increment
    )
    assert actions == (RedistributeGridProfits(pair=PAIR, amount_usd=Decimal("20.00")),)


def test_headroom_budget_uses_configured_percentage_and_rounds_down() -> None:
    assert headroom_budget(remaining_order_capacity=9, grid_headroom_pct=70) == 6


def test_detect_orphans_returns_grid_legs_older_than_timeout() -> None:
    slot = ManagedGridSlot(
        slot=GridSlot(
            pair=PAIR,
            a_leg=GridLeg(
                side=OrderSide.SELL,
                kind=GridLegKind.ENTRY,
                price=Decimal("0.22"),
                quantity=Decimal("50"),
            ),
            b_leg=GridLeg(
                side=OrderSide.BUY,
                kind=GridLegKind.ENTRY,
                price=Decimal("0.18"),
                quantity=Decimal("50"),
            ),
        ),
        a_opened_at=NOW - timedelta(minutes=10),
        b_opened_at=NOW - timedelta(minutes=2),
    )

    orphans = detect_orphans(
        GridEngineState(pair=PAIR, slots=(slot,), accepting_new_entries=True),
        now=NOW,
        timeout=timedelta(minutes=5),
    )

    assert orphans == (
        OrphanedGridLeg(
            slot_index=0,
            leg_name="a_leg",
            leg=GridLeg(
                side=OrderSide.SELL,
                kind=GridLegKind.ENTRY,
                price=Decimal("0.22"),
                quantity=Decimal("50"),
            ),
            opened_at=NOW - timedelta(minutes=10),
            age_seconds=600,
        ),
    )
