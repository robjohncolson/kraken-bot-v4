from __future__ import annotations

from decimal import Decimal

import pytest

from core.config import Settings, load_settings
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    Position,
    PositionSide,
    UpdateStop,
    UpdateTarget,
)
from trading.position import (
    InvalidBeliefForPositionError,
    InvalidTargetPriceError,
    PositionAlreadyClosedError,
    PositionAlreadyOpenError,
    PositionNotOpenError,
    close_position,
    open_position,
    update_stop,
    update_target,
)


PAIR = "DOGE/USD"


def _settings() -> Settings:
    return load_settings(
        {
            "KRAKEN_API_KEY": "key",
            "KRAKEN_API_SECRET": "secret",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_KEY": "supabase-key",
        }
    )


def _belief(direction: BeliefDirection) -> BeliefSnapshot:
    return BeliefSnapshot(pair=PAIR, direction=direction, confidence=0.8)


def _draft_position(side: PositionSide = PositionSide.LONG) -> Position:
    return Position(
        position_id="pos-1",
        pair=PAIR,
        side=side,
        quantity=Decimal("10"),
        entry_price=Decimal("100"),
        stop_price=Decimal("0"),
        target_price=Decimal("0"),
    )


def _opened_position(side: PositionSide = PositionSide.LONG) -> Position:
    direction = (
        BeliefDirection.BULLISH if side == PositionSide.LONG else BeliefDirection.BEARISH
    )
    position, _ = open_position(_draft_position(side=side), belief=_belief(direction), config=_settings())
    return position


@pytest.mark.parametrize(
    ("side", "direction", "expected_stop", "expected_target"),
    [
        (PositionSide.LONG, BeliefDirection.BULLISH, Decimal("95"), Decimal("110")),
        (PositionSide.SHORT, BeliefDirection.BEARISH, Decimal("105"), Decimal("90")),
    ],
)
def test_open_position_computes_stop_and_target_from_defaults(
    side: PositionSide,
    direction: BeliefDirection,
    expected_stop: Decimal,
    expected_target: Decimal,
) -> None:
    draft = _draft_position(side=side)

    opened, actions = open_position(draft, belief=_belief(direction), config=_settings())

    assert opened is not draft
    assert draft.stop_price == Decimal("0")
    assert draft.target_price == Decimal("0")
    assert opened.stop_price == expected_stop
    assert opened.target_price == expected_target
    assert actions == (
        UpdateStop(position_id="pos-1", stop_price=expected_stop),
        UpdateTarget(position_id="pos-1", target_price=expected_target),
    )


def test_close_position_zeroes_out_open_state_and_emits_close_action() -> None:
    opened = _opened_position()

    closed, actions = close_position(opened, reason=" belief_flip ")

    assert closed is not opened
    assert closed.quantity == Decimal("0")
    assert closed.stop_price == Decimal("0")
    assert closed.target_price == Decimal("0")
    assert closed.entry_price == opened.entry_price
    assert len(actions) == 1
    close_action = actions[0]
    assert close_action.position_id == "pos-1"
    assert close_action.reason == "belief_flip"
    assert close_action.pair == "DOGE/USD"
    assert close_action.side == PositionSide.LONG
    assert close_action.quantity == opened.quantity
    assert close_action.limit_price == opened.entry_price


def test_update_stop_returns_new_position_and_update_action() -> None:
    opened = _opened_position()

    updated, actions = update_stop(opened, stop_price=Decimal("96"))

    assert updated is not opened
    assert updated.stop_price == Decimal("96")
    assert updated.target_price == opened.target_price
    assert actions == (UpdateStop(position_id="pos-1", stop_price=Decimal("96")),)


def test_update_target_returns_new_position_and_update_action() -> None:
    opened = _opened_position()

    updated, actions = update_target(opened, target_price=Decimal("112"))

    assert updated is not opened
    assert updated.target_price == Decimal("112")
    assert updated.stop_price == opened.stop_price
    assert actions == (UpdateTarget(position_id="pos-1", target_price=Decimal("112")),)


@pytest.mark.parametrize(
    ("transition", "expected_error"),
    [
        (
            lambda: open_position(
                _opened_position(),
                belief=_belief(BeliefDirection.BULLISH),
                config=_settings(),
            ),
            PositionAlreadyOpenError,
        ),
        (
            lambda: close_position(_draft_position(), reason="manual"),
            PositionNotOpenError,
        ),
        (
            lambda: update_stop(
                Position(
                    position_id="pos-1",
                    pair=PAIR,
                    side=PositionSide.LONG,
                    quantity=Decimal("0"),
                    entry_price=Decimal("100"),
                    stop_price=Decimal("0"),
                    target_price=Decimal("0"),
                ),
                stop_price=Decimal("95"),
            ),
            PositionAlreadyClosedError,
        ),
        (
            lambda: open_position(
                _draft_position(side=PositionSide.LONG),
                belief=_belief(BeliefDirection.BEARISH),
                config=_settings(),
            ),
            InvalidBeliefForPositionError,
        ),
        (
            lambda: update_target(_opened_position(side=PositionSide.SHORT), target_price=Decimal("101")),
            InvalidTargetPriceError,
        ),
    ],
)
def test_invalid_transitions_are_rejected(
    transition: object,
    expected_error: type[Exception],
) -> None:
    with pytest.raises(expected_error):
        transition()  # type: ignore[operator]
