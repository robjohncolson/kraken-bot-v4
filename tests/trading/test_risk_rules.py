from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from core.config import Settings, load_settings
from core.types import ClosePosition, Portfolio, Position, PositionSide
from trading.risk_rules import (
    BlockNewEntries,
    CloseAllPositions,
    HardDrawdownViolation,
    HARD_DRAWDOWN_REASON,
    KellySizeViolation,
    MaxPositionsViolation,
    MissingStopLossViolation,
    NoAction,
    ReentryCooldownViolation,
    SameSideConcentrationViolation,
    SinglePairAllocationViolation,
    SoftDrawdownViolation,
    check_portfolio_rules,
    check_position_rules,
)


def _settings(**overrides: str) -> Settings:
    env = {
        "KRAKEN_API_KEY": "key",
        "KRAKEN_API_SECRET": "secret",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_KEY": "supabase-key",
    }
    env.update(overrides)
    return load_settings(env)


def _position(
    *,
    position_id: str,
    pair: str,
    side: PositionSide = PositionSide.LONG,
    quantity: str,
    entry_price: str,
    stop_price: str = "95",
    target_price: str = "110",
) -> Position:
    return Position(
        position_id=position_id,
        pair=pair,
        side=side,
        quantity=Decimal(quantity),
        entry_price=Decimal(entry_price),
        stop_price=Decimal(stop_price),
        target_price=Decimal(target_price),
    )


def _portfolio(
    *,
    cash_usd: str,
    positions: tuple[Position, ...],
    max_drawdown: str = "0.05",
) -> Portfolio:
    cash = Decimal(cash_usd)
    total_value = cash + sum(
        (
            position.quantity * position.entry_price
            if position.side == PositionSide.LONG
            else -(position.quantity * position.entry_price)
            for position in positions
        ),
        start=Decimal("0"),
    )
    return Portfolio(
        cash_usd=cash,
        positions=positions,
        total_value_usd=total_value,
        max_drawdown=Decimal(max_drawdown),
    )


def test_clean_position_and_portfolio_pass() -> None:
    settings = _settings()
    position = _position(
        position_id="btc-1",
        pair="BTC/USD",
        quantity="1",
        entry_price="100",
    )
    portfolio = _portfolio(cash_usd="900", positions=(position,))

    position_result = check_position_rules(
        position,
        portfolio_value_usd=Decimal("1000"),
        kelly_fraction_value=Decimal("0.20"),
        config=settings,
    )
    portfolio_result = check_portfolio_rules(portfolio, config=settings)

    assert position_result.passed is True
    assert position_result.violations == []
    assert position_result.recommended_action == NoAction()
    assert portfolio_result.passed is True
    assert portfolio_result.violations == []
    assert portfolio_result.recommended_action == NoAction()


def test_check_position_rules_flags_missing_stop_loss() -> None:
    result = check_position_rules(
        _position(
            position_id="btc-1",
            pair="BTC/USD",
            quantity="1",
            entry_price="100",
            stop_price="0",
        ),
        portfolio_value_usd=Decimal("1000"),
        kelly_fraction_value=Decimal("0.20"),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        MissingStopLossViolation(position_id="btc-1", pair="BTC/USD")
    ]
    assert result.recommended_action == NoAction()


def test_check_position_rules_flags_kelly_bound_violation() -> None:
    result = check_position_rules(
        _position(
            position_id="btc-1",
            pair="BTC/USD",
            quantity="2",
            entry_price="100",
        ),
        portfolio_value_usd=Decimal("1000"),
        kelly_fraction_value=Decimal("0.10"),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        KellySizeViolation(
            position_id="btc-1",
            pair="BTC/USD",
            size_usd=Decimal("200"),
            max_size_usd=Decimal("100"),
        )
    ]


def test_check_position_rules_flags_reentry_cooldown_violation() -> None:
    last_stop_loss_at = datetime(2026, 3, 23, 13, 0, 0)
    as_of = datetime(2026, 3, 24, 12, 0, 0)
    result = check_position_rules(
        _position(
            position_id="btc-1",
            pair="BTC/USD",
            quantity="1",
            entry_price="100",
        ),
        portfolio_value_usd=Decimal("1000"),
        kelly_fraction_value=Decimal("0.20"),
        config=_settings(),
        as_of=as_of,
        last_stop_loss_at=last_stop_loss_at,
    )

    assert result.passed is False
    assert result.violations == [
        ReentryCooldownViolation(
            pair="BTC/USD",
            last_stop_loss_at=last_stop_loss_at,
            blocked_until=datetime(2026, 3, 24, 13, 0, 0),
        )
    ]


def test_check_portfolio_rules_flags_max_positions_violation() -> None:
    positions = tuple(
        _position(
            position_id=f"pos-{index}",
            pair=f"PAIR-{index}/USD",
            quantity="1",
            entry_price="50",
        )
        for index in range(9)
    )

    result = check_portfolio_rules(
        _portfolio(cash_usd="1000", positions=positions),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [MaxPositionsViolation(open_positions=9, max_positions=8)]


def test_check_portfolio_rules_flags_same_side_concentration_violation() -> None:
    positions = tuple(
        _position(
            position_id=f"pos-{index}",
            pair=f"PAIR-{index}/USD",
            quantity="1",
            entry_price="130",
        )
        for index in range(5)
    )

    result = check_portfolio_rules(
        _portfolio(cash_usd="350", positions=positions),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        SameSideConcentrationViolation(
            side=PositionSide.LONG,
            concentration=Decimal("0.65"),
            limit=Decimal("0.6"),
        )
    ]


def test_check_portfolio_rules_flags_single_pair_allocation_violation() -> None:
    result = check_portfolio_rules(
        _portfolio(
            cash_usd="840",
            positions=(
                _position(
                    position_id="btc-1",
                    pair="BTC/USD",
                    quantity="1",
                    entry_price="160",
                ),
            ),
        ),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        SinglePairAllocationViolation(
            pair="BTC/USD",
            allocation=Decimal("0.16"),
            limit=Decimal("0.15"),
        )
    ]


def test_check_portfolio_rules_soft_drawdown_blocks_new_entries() -> None:
    position = _position(
        position_id="btc-1",
        pair="BTC/USD",
        quantity="1",
        entry_price="100",
    )

    result = check_portfolio_rules(
        _portfolio(cash_usd="900", positions=(position,), max_drawdown="0.10"),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        SoftDrawdownViolation(drawdown=Decimal("0.10"), limit=Decimal("0.10"))
    ]
    assert result.recommended_action == BlockNewEntries()


def test_check_portfolio_rules_hard_drawdown_closes_all_positions() -> None:
    positions = (
        _position(
            position_id="btc-1",
            pair="BTC/USD",
            quantity="1",
            entry_price="50",
        ),
        _position(
            position_id="eth-1",
            pair="ETH/USD",
            quantity="1",
            entry_price="50",
        ),
    )

    result = check_portfolio_rules(
        _portfolio(cash_usd="900", positions=positions, max_drawdown="0.15"),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        HardDrawdownViolation(
            drawdown=Decimal("0.15"),
            limit=Decimal("0.15"),
        )
    ]
    assert result.recommended_action == CloseAllPositions(
        actions=(
            ClosePosition(
                position_id="btc-1",
                reason=HARD_DRAWDOWN_REASON,
                pair="BTC/USD",
                side=PositionSide.LONG,
                quantity=Decimal("1"),
                limit_price=Decimal("50"),
            ),
            ClosePosition(
                position_id="eth-1",
                reason=HARD_DRAWDOWN_REASON,
                pair="ETH/USD",
                side=PositionSide.LONG,
                quantity=Decimal("1"),
                limit_price=Decimal("50"),
            ),
        )
    )


def test_check_portfolio_rules_returns_combined_violations() -> None:
    positions = tuple(
        _position(
            position_id=f"pos-{index}",
            pair="BTC/USD",
            quantity="1",
            entry_price="100",
        )
        for index in range(9)
    )

    result = check_portfolio_rules(
        _portfolio(cash_usd="100", positions=positions, max_drawdown="0.10"),
        config=_settings(),
    )

    assert result.passed is False
    assert result.violations == [
        MaxPositionsViolation(open_positions=9, max_positions=8),
        SameSideConcentrationViolation(
            side=PositionSide.LONG,
            concentration=Decimal("0.9"),
            limit=Decimal("0.6"),
        ),
        SinglePairAllocationViolation(
            pair="BTC/USD",
            allocation=Decimal("0.9"),
            limit=Decimal("0.15"),
        ),
        SoftDrawdownViolation(drawdown=Decimal("0.10"), limit=Decimal("0.10")),
    ]
    assert result.recommended_action == BlockNewEntries()
