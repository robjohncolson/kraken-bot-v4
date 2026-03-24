from __future__ import annotations

from decimal import Decimal

from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    OrderRequest,
    OrderSide,
    OrderType,
    PairAllocation,
    PlaceOrder,
    Portfolio,
    Position,
    PositionSide,
)
from trading.portfolio import (
    DEFAULT_DOGE_MAKER_OFFSET_PCT,
    DOGE_USD_PAIR,
    apply_close,
    apply_fill,
    compute_concentration,
    compute_directional_exposure,
)


def _position(
    *,
    position_id: str,
    pair: str,
    side: PositionSide,
    quantity: str,
    entry_price: str,
) -> Position:
    return Position(
        position_id=position_id,
        pair=pair,
        side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        stop_price=Decimal("1"),
        target_price=Decimal("2"),
    )


def _belief(direction: BeliefDirection) -> BeliefSnapshot:
    return BeliefSnapshot(pair=DOGE_USD_PAIR, direction=direction, confidence=0.9)


def test_apply_fill_adds_new_position_and_recomputes_metrics() -> None:
    portfolio = Portfolio(cash_usd=Decimal("1000"))
    position = _position(
        position_id="pos-1",
        pair="BTC/USD",
        side=PositionSide.LONG,
        quantity="2",
        entry_price="100",
    )

    updated = apply_fill(portfolio, position=position)

    assert updated is not portfolio
    assert portfolio.positions == ()
    assert portfolio.cash_usd == Decimal("1000")
    assert updated.cash_usd == Decimal("800")
    assert updated.total_value_usd == Decimal("1000")
    assert updated.positions == (position,)
    assert updated.concentration == (
        PairAllocation(pair="BTC/USD", percent=Decimal("0.2")),
    )
    assert updated.directional_exposure == Decimal("0.2")


def test_apply_fill_replaces_existing_position_with_incremental_cash_accounting() -> None:
    original = apply_fill(
        Portfolio(cash_usd=Decimal("1000")),
        position=_position(
            position_id="pos-1",
            pair="BTC/USD",
            side=PositionSide.LONG,
            quantity="1",
            entry_price="100",
        ),
    )
    modified_position = _position(
        position_id="pos-1",
        pair="BTC/USD",
        side=PositionSide.LONG,
        quantity="2",
        entry_price="110",
    )

    updated = apply_fill(original, position=modified_position)

    assert updated.positions == (modified_position,)
    assert updated.cash_usd == Decimal("780")
    assert updated.total_value_usd == Decimal("1000")
    assert len(updated.positions) == 1


def test_apply_close_removes_position_and_realizes_usd_proceeds() -> None:
    opened = apply_fill(
        Portfolio(cash_usd=Decimal("1000")),
        position=_position(
            position_id="pos-1",
            pair="BTC/USD",
            side=PositionSide.LONG,
            quantity="2",
            entry_price="100",
        ),
    )

    closed, actions = apply_close(
        opened,
        position_id="pos-1",
        close_price=Decimal("120"),
        doge_belief=_belief(BeliefDirection.NEUTRAL),
    )

    assert closed.positions == ()
    assert closed.cash_usd == Decimal("1040")
    assert closed.total_value_usd == Decimal("1040")
    assert closed.concentration == ()
    assert closed.directional_exposure == Decimal("0")
    assert actions == ()


def test_compute_concentration_aggregates_by_pair() -> None:
    portfolio = Portfolio(
        cash_usd=Decimal("800"),
        positions=(
            _position(
                position_id="btc-1",
                pair="BTC/USD",
                side=PositionSide.LONG,
                quantity="2",
                entry_price="100",
            ),
            _position(
                position_id="btc-2",
                pair="BTC/USD",
                side=PositionSide.LONG,
                quantity="1",
                entry_price="50",
            ),
            _position(
                position_id="eth-1",
                pair="ETH/USD",
                side=PositionSide.SHORT,
                quantity="1",
                entry_price="50",
            ),
        ),
    )

    assert compute_concentration(portfolio) == (
        PairAllocation(pair="BTC/USD", percent=Decimal("0.25")),
        PairAllocation(pair="ETH/USD", percent=Decimal("0.05")),
    )


def test_compute_directional_exposure_returns_net_long_minus_short_fraction() -> None:
    portfolio = Portfolio(
        cash_usd=Decimal("800"),
        positions=(
            _position(
                position_id="btc-1",
                pair="BTC/USD",
                side=PositionSide.LONG,
                quantity="2",
                entry_price="100",
            ),
            _position(
                position_id="btc-2",
                pair="BTC/USD",
                side=PositionSide.LONG,
                quantity="1",
                entry_price="50",
            ),
            _position(
                position_id="eth-1",
                pair="ETH/USD",
                side=PositionSide.SHORT,
                quantity="1",
                entry_price="50",
            ),
        ),
    )

    assert compute_directional_exposure(portfolio) == Decimal("0.2")


def test_apply_close_queues_doge_accumulation_order_when_doge_is_bullish() -> None:
    opened = apply_fill(
        Portfolio(cash_usd=Decimal("1000")),
        position=_position(
            position_id="pos-1",
            pair="BTC/USD",
            side=PositionSide.LONG,
            quantity="2",
            entry_price="100",
        ),
    )
    doge_market_price = Decimal("0.2500")
    expected_limit = doge_market_price * (
        (Decimal("100") - DEFAULT_DOGE_MAKER_OFFSET_PCT) / Decimal("100")
    )
    expected_quantity = Decimal("40") / expected_limit

    closed, actions = apply_close(
        opened,
        position_id="pos-1",
        close_price=Decimal("120"),
        doge_belief=_belief(BeliefDirection.BULLISH),
        doge_market_price=doge_market_price,
    )

    assert closed.cash_usd == Decimal("1040")
    assert actions == (
        PlaceOrder(
            order=OrderRequest(
                pair=DOGE_USD_PAIR,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=expected_quantity,
                limit_price=expected_limit,
            )
        ),
    )
