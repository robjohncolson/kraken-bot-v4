from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.config import Settings, load_settings
from core.state_machine import NO_ACTIONS, reduce
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefUpdate,
    BotState,
    ClosePosition,
    FillConfirmed,
    GridCycleComplete,
    LogEvent,
    MarketRegime,
    OrderSide,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PlaceOrder,
    Portfolio,
    Position,
    PositionSide,
    PriceTick,
    ReconciliationResult,
    StopTriggered,
    TargetHit,
)

NOW = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)


def _settings(**overrides: object) -> Settings:
    env = {
        "KRAKEN_API_KEY": "key",
        "KRAKEN_API_SECRET": "secret",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_KEY": "supabase-key",
    }
    env.update({k.upper(): str(v) for k, v in overrides.items()})
    return load_settings(env)


def _position(
    position_id: str = "pos-001",
    pair: str = "DOGE/USD",
    side: PositionSide = PositionSide.LONG,
    quantity: Decimal = Decimal("100"),
    entry_price: Decimal = Decimal("0.20"),
    stop_price: Decimal = Decimal("0.19"),
    target_price: Decimal = Decimal("0.22"),
) -> Position:
    return Position(
        position_id=position_id,
        pair=pair,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
    )


def _portfolio(
    positions: tuple[Position, ...] = (),
    cash_usd: Decimal = Decimal("100"),
) -> Portfolio:
    return Portfolio(
        cash_usd=cash_usd,
        positions=positions,
        total_value_usd=cash_usd + sum(p.quantity * p.entry_price for p in positions),
    )


def _bullish_beliefs(pair: str = "DOGE/USD") -> tuple[BeliefSnapshot, ...]:
    """Two bullish beliefs = consensus."""
    return (
        BeliefSnapshot(pair=pair, direction=BeliefDirection.BULLISH, confidence=0.8, regime=MarketRegime.TRENDING),
        BeliefSnapshot(pair=pair, direction=BeliefDirection.BULLISH, confidence=0.7, regime=MarketRegime.TRENDING),
    )


def _bearish_beliefs(pair: str = "DOGE/USD") -> tuple[BeliefSnapshot, ...]:
    return (
        BeliefSnapshot(pair=pair, direction=BeliefDirection.BEARISH, confidence=0.8, regime=MarketRegime.TRENDING),
        BeliefSnapshot(pair=pair, direction=BeliefDirection.BEARISH, confidence=0.7, regime=MarketRegime.TRENDING),
    )


# ---------------------------------------------------------------------------
# PriceTick — no-op
# ---------------------------------------------------------------------------


def test_reduce_returns_deterministic_no_op_for_initial_state() -> None:
    state = BotState()
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.12"))

    next_state, actions = reduce(state, event, _settings())

    assert next_state is state
    assert next_state == state
    assert actions == NO_ACTIONS


def test_reduce_is_pure_for_repeated_identical_inputs() -> None:
    state = BotState()
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.12"))
    settings = _settings()

    first_result = reduce(state, event, settings)
    second_result = reduce(state, event, settings)

    assert first_result == second_result
    assert first_result[0] is state
    assert second_result[0] is state
    assert state == BotState()


def test_price_tick_is_noop() -> None:
    state = BotState(as_of=NOW)
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.15"))
    new_state, actions = reduce(state, event, _settings())
    assert new_state is state
    assert actions == NO_ACTIONS


# ---------------------------------------------------------------------------
# StopTriggered
# ---------------------------------------------------------------------------


def test_stop_triggered_closes_position_and_records_cooldown() -> None:
    pos = _position()
    state = BotState(
        portfolio=_portfolio(positions=(pos,)),
        as_of=NOW,
    )
    event = StopTriggered(position_id="pos-001", trigger_price=Decimal("0.18"))

    new_state, actions = reduce(state, event, _settings())

    # Position should be removed from portfolio
    assert len(new_state.portfolio.positions) == 0
    # Cooldown recorded
    assert len(new_state.cooldowns) == 1
    assert new_state.cooldowns[0][0] == "DOGE/USD"
    # ClosePosition action emitted
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "stop_loss"


def test_stop_triggered_position_not_found_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = StopTriggered(position_id="nonexistent", trigger_price=Decimal("0.18"))

    new_state, actions = reduce(state, event, _settings())

    assert new_state is state
    assert len(actions) == 1
    assert isinstance(actions[0], LogEvent)
    assert "not found" in actions[0].message


# ---------------------------------------------------------------------------
# TargetHit
# ---------------------------------------------------------------------------


def test_target_hit_closes_position() -> None:
    pos = _position()
    state = BotState(
        portfolio=_portfolio(positions=(pos,)),
        as_of=NOW,
    )
    event = TargetHit(position_id="pos-001", trigger_price=Decimal("0.23"))

    new_state, actions = reduce(state, event, _settings())

    assert len(new_state.portfolio.positions) == 0
    # No cooldown for target hits
    assert len(new_state.cooldowns) == 0
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "target_hit"


def test_target_hit_position_not_found_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = TargetHit(position_id="nonexistent", trigger_price=Decimal("0.23"))

    new_state, actions = reduce(state, event, _settings())

    assert new_state is state
    assert len(actions) == 1
    assert isinstance(actions[0], LogEvent)


# ---------------------------------------------------------------------------
# BeliefUpdate — entry
# ---------------------------------------------------------------------------


def test_belief_update_opens_position_when_consensus_bullish() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")),
        beliefs=_bullish_beliefs(),
        as_of=NOW,
    )
    new_belief = BeliefSnapshot(
        pair="DOGE/USD",
        direction=BeliefDirection.BULLISH,
        confidence=0.9,
        regime=MarketRegime.TRENDING,
    )
    event = BeliefUpdate(belief=new_belief)

    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 1
    assert place_orders[0].order.pair == "DOGE/USD"
    assert place_orders[0].order.side == OrderSide.BUY
    # Position seq incremented
    assert new_state.next_position_seq == 1
    # Pending order recorded
    assert len(new_state.pending_orders) == 1


def test_belief_update_no_entry_when_neutral() -> None:
    neutral_beliefs = (
        BeliefSnapshot(pair="DOGE/USD", direction=BeliefDirection.NEUTRAL, confidence=0.5),
    )
    state = BotState(
        portfolio=_portfolio(),
        beliefs=neutral_beliefs,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.NEUTRAL, confidence=0.5,
    ))

    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 0


def test_belief_update_closes_position_on_flip() -> None:
    pos = _position(side=PositionSide.LONG)
    state = BotState(
        portfolio=_portfolio(positions=(pos,)),
        beliefs=_bearish_beliefs(),
        as_of=NOW,
    )
    # Bearish belief flips against the long position
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9,
        regime=MarketRegime.TRENDING,
    ))

    new_state, actions = reduce(state, event, _settings())

    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "belief_change"
    assert len(new_state.portfolio.positions) == 0


def test_belief_update_blocked_by_entry_blocked() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")),
        beliefs=_bullish_beliefs(),
        as_of=NOW,
        entry_blocked=True,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9,
        regime=MarketRegime.TRENDING,
    ))

    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 0
    log_actions = [a for a in actions if isinstance(a, LogEvent)]
    assert any("entry blocked" in a.message for a in log_actions)


def test_belief_update_blocked_by_cooldown() -> None:
    cooldown_ts = NOW.isoformat()
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")),
        beliefs=_bullish_beliefs(),
        as_of=NOW + timedelta(hours=1),  # only 1 hour after stop, cooldown is 24h
        cooldowns=(("DOGE/USD", cooldown_ts),),
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9,
        regime=MarketRegime.TRENDING,
    ))

    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 0
    log_actions = [a for a in actions if isinstance(a, LogEvent)]
    assert any("cooldown" in a.message for a in log_actions)


def test_belief_update_blocked_by_max_positions() -> None:
    # Fill portfolio with max_positions (default 8) positions
    positions = tuple(
        _position(position_id=f"pos-{i:03d}", pair=f"PAIR{i}/USD")
        for i in range(8)
    )
    state = BotState(
        portfolio=_portfolio(positions=positions, cash_usd=Decimal("1000")),
        beliefs=_bullish_beliefs(),
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9,
        regime=MarketRegime.TRENDING,
    ))

    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 0
    log_actions = [a for a in actions if isinstance(a, LogEvent)]
    assert any("max positions" in a.message for a in log_actions)


# ---------------------------------------------------------------------------
# FillConfirmed
# ---------------------------------------------------------------------------


def test_fill_confirmed_updates_portfolio() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")),
        beliefs=_bullish_beliefs(),
        pending_orders=(("kbv4-dogeusd-000000-entry", "kbv4-dogeusd-000000"),),
        open_orders=(
            OrderSnapshot(
                order_id="TXID-001",
                pair="DOGE/USD",
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                status=OrderStatus.OPEN,
                quantity=Decimal("50"),
                client_order_id="kbv4-dogeusd-000000-entry",
            ),
        ),
        as_of=NOW,
    )
    event = FillConfirmed(
        order_id="TXID-001",
        pair="DOGE/USD",
        filled_quantity=Decimal("50"),
        fill_price=Decimal("0.20"),
    )

    new_state, actions = reduce(state, event, _settings())

    assert len(new_state.portfolio.positions) == 1
    pos = new_state.portfolio.positions[0]
    assert pos.pair == "DOGE/USD"
    assert pos.quantity == Decimal("50")
    assert pos.entry_price == Decimal("0.20")
    # Pending order consumed
    assert len(new_state.pending_orders) == 0
    log_actions = [a for a in actions if isinstance(a, LogEvent)]
    assert any("fill_confirmed" in a.message for a in log_actions)


def test_fill_confirmed_unknown_order_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = FillConfirmed(
        order_id="UNKNOWN",
        pair="DOGE/USD",
        filled_quantity=Decimal("50"),
        fill_price=Decimal("0.20"),
    )

    new_state, actions = reduce(state, event, _settings())

    assert new_state is state
    assert len(actions) == 1
    assert isinstance(actions[0], LogEvent)
    assert "no pending order" in actions[0].message


# ---------------------------------------------------------------------------
# ReconciliationResult
# ---------------------------------------------------------------------------


def test_reconciliation_soft_drawdown_blocks_entries() -> None:
    # Create portfolio with high drawdown to trigger soft limit
    portfolio = Portfolio(
        cash_usd=Decimal("100"),
        total_value_usd=Decimal("100"),
        max_drawdown=Decimal("0.12"),  # 12% > soft limit of 10%
    )
    state = BotState(portfolio=portfolio, as_of=NOW)
    event = ReconciliationResult(
        balances=(),
        open_orders=(),
        discrepancy_detected=False,
        summary="ok",
    )

    new_state, actions = reduce(state, event, _settings())

    assert new_state.entry_blocked is True
    log_actions = [a for a in actions if isinstance(a, LogEvent)]
    assert any("soft drawdown" in a.message for a in log_actions)


def test_reconciliation_hard_drawdown_closes_all() -> None:
    pos = _position()
    portfolio = Portfolio(
        cash_usd=Decimal("50"),
        positions=(pos,),
        total_value_usd=Decimal("70"),
        max_drawdown=Decimal("0.20"),  # 20% > hard limit of 15%
    )
    state = BotState(portfolio=portfolio, as_of=NOW)
    event = ReconciliationResult(
        balances=(),
        open_orders=(),
        discrepancy_detected=True,
        summary="hard drawdown",
    )

    new_state, actions = reduce(state, event, _settings())

    assert new_state.entry_blocked is True
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].position_id == "pos-001"


def test_reconciliation_clears_entry_block_when_healthy() -> None:
    state = BotState(
        portfolio=_portfolio(),
        as_of=NOW,
        entry_blocked=True,
    )
    event = ReconciliationResult(
        balances=(),
        open_orders=(),
        discrepancy_detected=False,
        summary="ok",
    )

    new_state, actions = reduce(state, event, _settings())

    assert new_state.entry_blocked is False


# ---------------------------------------------------------------------------
# GridCycleComplete — no-op for MVP
# ---------------------------------------------------------------------------


def test_grid_cycle_complete_logs() -> None:
    state = BotState(as_of=NOW)
    event = GridCycleComplete(pair="DOGE/USD", realized_pnl_usd=Decimal("1.50"))

    new_state, actions = reduce(state, event, _settings())

    assert new_state is state
    assert len(actions) == 1
    assert isinstance(actions[0], LogEvent)
    assert "grid_cycle_complete" in actions[0].message
