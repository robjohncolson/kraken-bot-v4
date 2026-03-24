from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from core.errors import KrakenBotError
from core.types import (
    Action,
    DeactivateGrid,
    LogEvent,
    OrderRequest,
    OrderSide,
    OrderType,
    Pair,
    PlaceOrder,
    Price,
    Quantity,
    RedistributeGridProfits,
    UsdAmount,
    ZERO_DECIMAL,
)
from grid.sizing import InvalidReferencePriceError, SlotAllocation, calculate_slot_count
from grid.states import GridLeg, GridLegKind, GridSlot


class GridEngineError(KrakenBotError):
    """Base exception for grid engine orchestration failures."""


class GridAlreadyActiveError(GridEngineError):
    """Raised when activation is requested for a grid that already has live slots."""

    def __init__(self, pair: Pair) -> None:
        self.pair = pair
        super().__init__(f"Grid for pair {pair!r} is already active or winding down.")


class GridNotActiveError(GridEngineError):
    """Raised when deactivation is requested without an active grid."""

    def __init__(self, pair: Pair) -> None:
        self.pair = pair
        super().__init__(f"Grid for pair {pair!r} is not active.")


class InvalidHeadroomPercentageError(GridEngineError):
    """Raised when GRID_HEADROOM_PCT falls outside the inclusive 0-100 range."""

    def __init__(self, grid_headroom_pct: int) -> None:
        self.grid_headroom_pct = grid_headroom_pct
        super().__init__(
            f"GRID_HEADROOM_PCT must be between 0 and 100 inclusive; got {grid_headroom_pct}."
        )


class NegativeOrderCapacityError(GridEngineError):
    """Raised when remaining Kraken order capacity is negative."""

    def __init__(self, remaining_order_capacity: int) -> None:
        self.remaining_order_capacity = remaining_order_capacity
        super().__init__(
            f"Remaining order capacity must be non-negative; got {remaining_order_capacity}."
        )


class NegativeProfitRedistributionError(GridEngineError):
    """Raised when profit redistribution is attempted with a negative realized amount."""

    def __init__(self, realized_profit_usd: UsdAmount) -> None:
        self.realized_profit_usd = realized_profit_usd
        super().__init__(
            f"Grid profit redistribution requires a non-negative amount; got {realized_profit_usd}."
        )


class InvalidOrphanTimeoutError(GridEngineError):
    """Raised when orphan detection is requested with a non-positive timeout."""

    def __init__(self, timeout: timedelta) -> None:
        self.timeout = timeout
        super().__init__(f"Orphan timeout must be positive; got {timeout}.")


@dataclass(frozen=True, slots=True)
class GridPricePlan:
    upper_entry_price: Price
    lower_entry_price: Price
    upper_exit_price: Price
    lower_exit_price: Price


@dataclass(frozen=True, slots=True)
class ManagedGridSlot:
    slot: GridSlot
    a_opened_at: datetime | None = None
    b_opened_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class OrphanedGridLeg:
    slot_index: int
    leg_name: str
    leg: GridLeg
    opened_at: datetime
    age_seconds: int


@dataclass(frozen=True, slots=True)
class GridEngineState:
    pair: Pair
    slots: tuple[ManagedGridSlot, ...] = ()
    accepting_new_entries: bool = False
    pending_profit_usd: UsdAmount = ZERO_DECIMAL
    allocation: SlotAllocation | None = None
    price_plan: GridPricePlan | None = None


def activate_grid(
    state: GridEngineState,
    *,
    available_capital_usd: UsdAmount,
    reference_price: Price,
    price_plan: GridPricePlan,
    remaining_order_capacity: int,
    grid_headroom_pct: int,
    now: datetime,
    client_order_id_prefix: str = "grid",
) -> tuple[GridEngineState, tuple[Action, ...]]:
    """Create minimum-sized grid slots and emit their initial entry orders."""

    if _has_live_grid(state):
        raise GridAlreadyActiveError(state.pair)

    sizing = calculate_slot_count(
        available_capital_usd=available_capital_usd,
        pair=state.pair,
        reference_price=reference_price,
    )
    budgeted_orders = headroom_budget(
        remaining_order_capacity=remaining_order_capacity,
        grid_headroom_pct=grid_headroom_pct,
    )
    activatable_slots = min(sizing.slot_count, budgeted_orders // 2)
    allocation = SlotAllocation(
        pair=sizing.pair,
        available_capital_usd=sizing.available_capital_usd,
        minimum_quantity=sizing.minimum_quantity,
        minimum_slot_size_usd=sizing.minimum_slot_size_usd,
        slot_count=activatable_slots,
        allocated_capital_usd=sizing.minimum_slot_size_usd * activatable_slots,
        remainder_usd=sizing.available_capital_usd
        - (sizing.minimum_slot_size_usd * activatable_slots),
    )

    slots: list[ManagedGridSlot] = []
    actions: list[Action] = []
    for slot_index in range(activatable_slots):
        pair_slug = state.pair.replace("/", "-").lower()
        a_client_order_id = f"{client_order_id_prefix}:{pair_slug}:{slot_index}:a"
        b_client_order_id = f"{client_order_id_prefix}:{pair_slug}:{slot_index}:b"
        slot = GridSlot(
            pair=state.pair,
            a_leg=GridLeg(
                side=OrderSide.SELL,
                kind=GridLegKind.ENTRY,
                price=price_plan.upper_entry_price,
                quantity=allocation.minimum_quantity,
                client_order_id=a_client_order_id,
            ),
            b_leg=GridLeg(
                side=OrderSide.BUY,
                kind=GridLegKind.ENTRY,
                price=price_plan.lower_entry_price,
                quantity=allocation.minimum_quantity,
                client_order_id=b_client_order_id,
            ),
        )
        slots.append(ManagedGridSlot(slot=slot, a_opened_at=now, b_opened_at=now))
        actions.append(
            PlaceOrder(
                order=OrderRequest(
                    pair=state.pair,
                    side=OrderSide.SELL,
                    order_type=OrderType.LIMIT,
                    quantity=allocation.minimum_quantity,
                    limit_price=price_plan.upper_entry_price,
                    client_order_id=a_client_order_id,
                )
            )
        )
        actions.append(
            PlaceOrder(
                order=OrderRequest(
                    pair=state.pair,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=allocation.minimum_quantity,
                    limit_price=price_plan.lower_entry_price,
                    client_order_id=b_client_order_id,
                )
            )
        )

    return (
        GridEngineState(
            pair=state.pair,
            slots=tuple(slots),
            accepting_new_entries=activatable_slots > 0,
            pending_profit_usd=state.pending_profit_usd,
            allocation=allocation,
            price_plan=price_plan,
        ),
        tuple(actions),
    )


def deactivate_grid(state: GridEngineState) -> tuple[GridEngineState, tuple[Action, ...]]:
    """Stop placing new entries and leave any working orders untouched."""

    if not _has_live_grid(state):
        raise GridNotActiveError(state.pair)

    return (
        replace(state, accepting_new_entries=False),
        (
            DeactivateGrid(pair=state.pair),
            LogEvent(
                message=(
                    f"Grid deactivated for {state.pair}; existing working orders remain live."
                )
            ),
        ),
    )


def redistribute_profits(
    state: GridEngineState,
    *,
    realized_profit_usd: UsdAmount,
    reference_price: Price,
) -> tuple[GridEngineState, tuple[Action, ...]]:
    """Compound accumulated profits evenly across the currently active slots."""

    if realized_profit_usd < ZERO_DECIMAL:
        raise NegativeProfitRedistributionError(realized_profit_usd)
    if reference_price <= ZERO_DECIMAL:
        raise InvalidReferencePriceError(state.pair, reference_price)

    total_profit = state.pending_profit_usd + realized_profit_usd
    if total_profit == ZERO_DECIMAL or not state.slots:
        return replace(state, pending_profit_usd=total_profit), ()

    quantity_increment = (total_profit / len(state.slots)) / reference_price
    updated_slots = tuple(
        replace(managed_slot, slot=_increase_slot_quantity(managed_slot.slot, quantity_increment))
        for managed_slot in state.slots
    )
    return (
        replace(state, slots=updated_slots, pending_profit_usd=ZERO_DECIMAL),
        (RedistributeGridProfits(pair=state.pair, amount_usd=total_profit),),
    )


def headroom_budget(*, remaining_order_capacity: int, grid_headroom_pct: int) -> int:
    """Return the number of grid orders allowed under the configured headroom policy."""

    if remaining_order_capacity < 0:
        raise NegativeOrderCapacityError(remaining_order_capacity)
    if not 0 <= grid_headroom_pct <= 100:
        raise InvalidHeadroomPercentageError(grid_headroom_pct)
    return (remaining_order_capacity * grid_headroom_pct) // 100


def detect_orphans(
    state: GridEngineState,
    *,
    now: datetime,
    timeout: timedelta,
) -> tuple[OrphanedGridLeg, ...]:
    """Return any grid legs whose timestamps exceed the configured orphan timeout."""

    if timeout <= timedelta(0):
        raise InvalidOrphanTimeoutError(timeout)

    orphans: list[OrphanedGridLeg] = []
    for slot_index, managed_slot in enumerate(state.slots):
        for leg_name, leg, opened_at in (
            ("a_leg", managed_slot.slot.a_leg, managed_slot.a_opened_at),
            ("b_leg", managed_slot.slot.b_leg, managed_slot.b_opened_at),
        ):
            if leg is None or opened_at is None or opened_at > now:
                continue
            age_seconds = int((now - opened_at).total_seconds())
            if age_seconds >= int(timeout.total_seconds()):
                orphans.append(
                    OrphanedGridLeg(
                        slot_index=slot_index,
                        leg_name=leg_name,
                        leg=leg,
                        opened_at=opened_at,
                        age_seconds=age_seconds,
                    )
                )
    return tuple(orphans)


def _has_live_grid(state: GridEngineState) -> bool:
    return state.accepting_new_entries or bool(state.slots)


def _increase_slot_quantity(slot: GridSlot, quantity_increment: Quantity) -> GridSlot:
    return GridSlot(
        pair=slot.pair,
        a_leg=_increase_leg_quantity(slot.a_leg, quantity_increment),
        b_leg=_increase_leg_quantity(slot.b_leg, quantity_increment),
    )


def _increase_leg_quantity(leg: GridLeg | None, quantity_increment: Quantity) -> GridLeg | None:
    if leg is None:
        return None
    return replace(leg, quantity=leg.quantity + quantity_increment)


__all__ = [
    "activate_grid",
    "deactivate_grid",
    "detect_orphans",
    "GridAlreadyActiveError",
    "GridEngineError",
    "GridEngineState",
    "GridNotActiveError",
    "GridPricePlan",
    "headroom_budget",
    "InvalidHeadroomPercentageError",
    "InvalidOrphanTimeoutError",
    "ManagedGridSlot",
    "NegativeOrderCapacityError",
    "NegativeProfitRedistributionError",
    "OrphanedGridLeg",
    "redistribute_profits",
]
