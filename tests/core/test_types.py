from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from core.types import (
    BotState,
    EventType,
    GridPhase,
    GridState,
    OrderRequest,
    OrderSide,
    OrderType,
    PairAllocation,
    Portfolio,
    Position,
    PositionSide,
)


def test_order_request_is_frozen() -> None:
    order = OrderRequest(
        pair="DOGE/USD",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        limit_price=Decimal("0.12"),
        client_order_id="kb4-123",
    )

    with pytest.raises(FrozenInstanceError):
        order.quantity = Decimal("200")


def test_portfolio_and_state_use_immutable_models() -> None:
    position = Position(
        position_id="pos-1",
        pair="DOGE/USD",
        side=PositionSide.LONG,
        quantity=Decimal("100"),
        entry_price=Decimal("0.12"),
        stop_price=Decimal("0.10"),
        target_price=Decimal("0.15"),
        grid_state=GridState(
            phase=GridPhase.S0,
            active_slot_count=2,
            accepting_new_entries=True,
            realized_pnl_usd=Decimal("1.25"),
        ),
    )
    portfolio = Portfolio(
        cash_usd=Decimal("50"),
        cash_doge=Decimal("100"),
        positions=(position,),
        total_value_usd=Decimal("62"),
        concentration=(PairAllocation(pair="DOGE/USD", percent=Decimal("1.0")),),
        directional_exposure=Decimal("1.0"),
        max_drawdown=Decimal("0.05"),
    )
    state = BotState(portfolio=portfolio)

    with pytest.raises(FrozenInstanceError):
        portfolio.cash_usd = Decimal("0")

    with pytest.raises(FrozenInstanceError):
        state.last_event = EventType.PRICE_TICK

    with pytest.raises(AttributeError):
        state.portfolio.positions.append(position)  # type: ignore[attr-defined]

    assert state.portfolio.positions[0].grid_state is not None
    assert state.portfolio.positions[0].grid_state.phase == GridPhase.S0
