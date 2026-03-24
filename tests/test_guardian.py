from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.config import Settings, load_settings
from core.types import Portfolio, Position, PositionSide
from guardian import Guardian, GuardianAction, GuardianActionType, PriceSnapshot
from trading.risk_rules import BlockNewEntries, SoftDrawdownViolation


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
    quantity: str = "1",
    entry_price: str = "100",
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


def test_check_positions_flags_stop_hit_and_attempts_limit_exit() -> None:
    guardian = Guardian()
    portfolio = _portfolio(
        cash_usd="900",
        positions=(
            _position(position_id="btc-1", pair="BTC/USD"),
        ),
    )

    actions = guardian.check_positions(
        {"BTC/USD": Decimal("94")},
        portfolio,
        _settings(),
    )

    assert actions == [
        GuardianAction(
            action_type=GuardianActionType.STOP_TRIGGERED,
            details={
                "position_id": "btc-1",
                "pair": "BTC/USD",
                "trigger_price": Decimal("94"),
                "stop_price": Decimal("95"),
            },
        ),
        GuardianAction(
            action_type=GuardianActionType.LIMIT_EXIT_ATTEMPT,
            details={
                "position_id": "btc-1",
                "pair": "BTC/USD",
                "trigger_price": Decimal("94"),
                "reason": "stop_triggered",
            },
        ),
    ]


def test_check_positions_flags_target_hit_and_attempts_limit_exit() -> None:
    guardian = Guardian()
    portfolio = _portfolio(
        cash_usd="1100",
        positions=(
            _position(
                position_id="eth-1",
                pair="ETH/USD",
                side=PositionSide.SHORT,
                stop_price="105",
                target_price="90",
            ),
        ),
    )

    actions = guardian.check_positions(
        {"ETH/USD": Decimal("89")},
        portfolio,
        _settings(),
    )

    assert actions == [
        GuardianAction(
            action_type=GuardianActionType.TARGET_HIT,
            details={
                "position_id": "eth-1",
                "pair": "ETH/USD",
                "trigger_price": Decimal("89"),
                "target_price": Decimal("90"),
            },
        ),
        GuardianAction(
            action_type=GuardianActionType.LIMIT_EXIT_ATTEMPT,
            details={
                "position_id": "eth-1",
                "pair": "ETH/USD",
                "trigger_price": Decimal("89"),
                "reason": "target_hit",
            },
        ),
    ]


def test_check_positions_flags_stale_belief() -> None:
    as_of = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)
    guardian = Guardian(clock=lambda: as_of)
    portfolio = _portfolio(
        cash_usd="900",
        positions=(
            _position(position_id="btc-1", pair="BTC/USD"),
        ),
    )

    actions = guardian.check_positions(
        {
            "BTC/USD": PriceSnapshot(
                price=Decimal("100"),
                belief_timestamp=as_of - timedelta(hours=5),
            )
        },
        portfolio,
        _settings(),
    )

    assert actions == [
        GuardianAction(
            action_type=GuardianActionType.BELIEF_STALE,
            details={
                "position_id": "btc-1",
                "pair": "BTC/USD",
                "belief_timestamp": as_of - timedelta(hours=5),
                "checked_at": as_of,
                "stale_after_hours": 4,
            },
        )
    ]


def test_check_positions_returns_no_actions_for_clean_portfolio() -> None:
    guardian = Guardian()
    portfolio = _portfolio(
        cash_usd="900",
        positions=(
            _position(position_id="btc-1", pair="BTC/USD"),
        ),
    )

    actions = guardian.check_positions(
        {"BTC/USD": Decimal("100")},
        portfolio,
        _settings(),
    )

    assert actions == []


def test_check_positions_accumulates_multiple_simultaneous_triggers() -> None:
    as_of = datetime(2026, 3, 24, 12, 0, tzinfo=timezone.utc)
    guardian = Guardian(clock=lambda: as_of)
    portfolio = _portfolio(
        cash_usd="900",
        positions=(
            _position(position_id="btc-1", pair="BTC/USD"),
            _position(
                position_id="eth-1",
                pair="ETH/USD",
                side=PositionSide.SHORT,
                stop_price="105",
                target_price="90",
            ),
            _position(position_id="doge-1", pair="DOGE/USD"),
        ),
        max_drawdown="0.10",
    )

    actions = guardian.check_positions(
        {
            "BTC/USD": Decimal("94"),
            "ETH/USD": Decimal("89"),
            "DOGE/USD": PriceSnapshot(
                price=Decimal("100"),
                belief_timestamp=as_of - timedelta(hours=5),
            ),
        },
        portfolio,
        _settings(),
    )

    assert [action.action_type for action in actions] == [
        GuardianActionType.STOP_TRIGGERED,
        GuardianActionType.LIMIT_EXIT_ATTEMPT,
        GuardianActionType.TARGET_HIT,
        GuardianActionType.LIMIT_EXIT_ATTEMPT,
        GuardianActionType.BELIEF_STALE,
        GuardianActionType.RISK_VIOLATION,
    ]
    assert actions[-1] == GuardianAction(
        action_type=GuardianActionType.RISK_VIOLATION,
        details={
            "violation": SoftDrawdownViolation(
                drawdown=Decimal("0.10"),
                limit=Decimal("0.10"),
            ),
            "violation_type": "SoftDrawdownViolation",
            "recommended_action": BlockNewEntries(),
        },
    )
