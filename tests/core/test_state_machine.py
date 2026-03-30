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
    PendingOrder,
    PlaceOrder,
    Portfolio,
    Position,
    PositionSide,
    PriceTick,
    ReconciliationResult,
    StopTriggered,
    TargetHit,
    WindowExpired,
    ZERO_DECIMAL,
)

NOW = datetime(2026, 3, 25, 12, 0, 0, tzinfo=timezone.utc)
DOGE_PRICE = Decimal("0.10")
DOGE_REF_PRICES = (("DOGE/USD", DOGE_PRICE),)


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
        position_id=position_id, pair=pair, side=side,
        quantity=quantity, entry_price=entry_price,
        stop_price=stop_price, target_price=target_price,
    )


def _portfolio(
    positions: tuple[Position, ...] = (),
    cash_usd: Decimal = Decimal("100"),
    cash_doge: Decimal = ZERO_DECIMAL,
) -> Portfolio:
    return Portfolio(
        cash_usd=cash_usd,
        cash_doge=cash_doge,
        positions=positions,
        total_value_usd=cash_usd + sum(p.quantity * p.entry_price for p in positions) + cash_doge * DOGE_PRICE,
    )


def _bullish_beliefs(pair: str = "DOGE/USD") -> tuple[BeliefSnapshot, ...]:
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
# PriceTick
# ---------------------------------------------------------------------------


def test_reduce_returns_deterministic_no_op_for_initial_state() -> None:
    state = BotState()
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.12"))
    next_state, actions = reduce(state, event, _settings())
    assert next_state is state
    assert actions == NO_ACTIONS


def test_reduce_is_pure_for_repeated_identical_inputs() -> None:
    state = BotState()
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.12"))
    settings = _settings()
    first = reduce(state, event, settings)
    second = reduce(state, event, settings)
    assert first == second
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
    state = BotState(portfolio=_portfolio(positions=(pos,)), as_of=NOW)
    event = StopTriggered(position_id="pos-001", trigger_price=Decimal("0.18"))
    new_state, actions = reduce(state, event, _settings())
    assert len(new_state.portfolio.positions) == 0
    assert len(new_state.cooldowns) == 1
    assert new_state.cooldowns[0][0] == "DOGE/USD"
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "stop_loss"


def test_stop_triggered_position_not_found_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = StopTriggered(position_id="nonexistent", trigger_price=Decimal("0.18"))
    new_state, actions = reduce(state, event, _settings())
    assert new_state is state
    assert isinstance(actions[0], LogEvent)
    assert "not found" in actions[0].message


# ---------------------------------------------------------------------------
# TargetHit
# ---------------------------------------------------------------------------


def test_target_hit_closes_position() -> None:
    pos = _position()
    state = BotState(portfolio=_portfolio(positions=(pos,)), as_of=NOW)
    event = TargetHit(position_id="pos-001", trigger_price=Decimal("0.23"))
    new_state, actions = reduce(state, event, _settings())
    assert len(new_state.portfolio.positions) == 0
    assert len(new_state.cooldowns) == 0
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "target_hit"


def test_target_hit_position_not_found_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = TargetHit(position_id="nonexistent", trigger_price=Decimal("0.23"))
    new_state, actions = reduce(state, event, _settings())
    assert new_state is state
    assert isinstance(actions[0], LogEvent)


def test_window_expired_closes_rotated_position_without_cooldown() -> None:
    pos = _position(pair="BTC/USD")
    state = BotState(
        portfolio=_portfolio(positions=(pos,)),
        beliefs=_bullish_beliefs(pair="DOGE/USD"),
        reference_prices=(("BTC/USD", Decimal("0.24")), ("DOGE/USD", DOGE_PRICE)),
        as_of=NOW,
    )
    event = WindowExpired(
        pair="BTC/USD",
        position_id="pos-001",
        trigger_price=Decimal("0.24"),
        expired_at=NOW,
    )
    new_state, actions = reduce(state, event, _settings())
    assert len(new_state.portfolio.positions) == 0
    assert len(new_state.cooldowns) == 0
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "window_expired"


def test_window_expired_position_not_found_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = WindowExpired(pair="BTC/USD", position_id="missing", expired_at=NOW)
    new_state, actions = reduce(state, event, _settings())
    assert new_state is state
    assert isinstance(actions[0], LogEvent)
    assert "not found" in actions[0].message


# ---------------------------------------------------------------------------
# BeliefUpdate — bullish buy entry
# ---------------------------------------------------------------------------


def test_belief_update_opens_position_when_consensus_bullish() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")),
        beliefs=_bullish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 1
    assert place_orders[0].order.pair == "DOGE/USD"
    assert place_orders[0].order.side == OrderSide.BUY
    # Quantity is base-asset (DOGE), not USD
    assert place_orders[0].order.quantity == Decimal("10") / DOGE_PRICE  # $10 min / $0.10
    assert new_state.next_position_seq == 1
    assert len(new_state.pending_orders) == 1
    assert new_state.pending_orders[0].kind == "position_entry"


def test_buy_order_quantity_is_base_asset() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("50")),
        beliefs=_bullish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 1
    # $10 min position / $0.10 price = 100 DOGE
    assert place_orders[0].order.quantity == Decimal("100")
    assert place_orders[0].order.limit_price == DOGE_PRICE


def test_bullish_entry_capped_by_free_usd() -> None:
    """Buy amount cannot exceed available USD — has DOGE equity but only $5 free USD."""
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("5"), cash_doge=Decimal("1000")),  # $105 total but only $5 free USD
        beliefs=_bullish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 0
    log_actions = [a for a in actions if isinstance(a, LogEvent)]
    assert any("insufficient free USD" in a.message for a in log_actions)


def test_belief_update_no_entry_when_neutral() -> None:
    state = BotState(
        portfolio=_portfolio(), beliefs=(
            BeliefSnapshot(pair="DOGE/USD", direction=BeliefDirection.NEUTRAL, confidence=0.5),
        ), as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(pair="DOGE/USD", direction=BeliefDirection.NEUTRAL, confidence=0.5))
    _, actions = reduce(state, event, _settings())
    assert not [a for a in actions if isinstance(a, PlaceOrder)]


def test_belief_update_closes_position_on_flip() -> None:
    pos = _position(side=PositionSide.LONG)
    state = BotState(portfolio=_portfolio(positions=(pos,)), beliefs=_bearish_beliefs(), as_of=NOW)
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    new_state, actions = reduce(state, event, _settings())
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1
    assert close_actions[0].reason == "belief_change"


def test_belief_update_blocked_by_entry_blocked() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")), beliefs=_bullish_beliefs(),
        reference_prices=DOGE_REF_PRICES, as_of=NOW, entry_blocked=True,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    assert not [a for a in actions if isinstance(a, PlaceOrder)]
    assert any("entry blocked" in a.message for a in actions if isinstance(a, LogEvent))


def test_belief_update_blocked_by_cooldown() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")), beliefs=_bullish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW + timedelta(hours=1), cooldowns=(("DOGE/USD", NOW.isoformat()),),
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    assert not [a for a in actions if isinstance(a, PlaceOrder)]
    assert any("cooldown" in a.message for a in actions if isinstance(a, LogEvent))


def test_belief_update_no_reference_price_returns_log() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")), beliefs=_bullish_beliefs(),
        as_of=NOW,  # no reference_prices
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BULLISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    assert not [a for a in actions if isinstance(a, PlaceOrder)]
    assert any("no reference price" in a.message for a in actions if isinstance(a, LogEvent))


# ---------------------------------------------------------------------------
# BeliefUpdate — bearish DOGE inventory sell
# ---------------------------------------------------------------------------


def test_bearish_belief_sells_doge_inventory() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("5000")),
        beliefs=_bearish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    new_state, actions = reduce(state, event, _settings())

    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert len(place_orders) == 1
    assert place_orders[0].order.side == OrderSide.SELL
    assert place_orders[0].order.quantity == Decimal("10") / DOGE_PRICE  # $10 min / $0.10
    # No Position created
    assert len(new_state.portfolio.positions) == 0
    # PendingOrder is inventory_sell
    assert len(new_state.pending_orders) == 1
    assert new_state.pending_orders[0].kind == "inventory_sell"


def test_sell_order_quantity_is_base_asset() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("5000")),
        beliefs=_bearish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    order = [a for a in actions if isinstance(a, PlaceOrder)][0].order
    # $10 / $0.10 = 100 DOGE
    assert order.quantity == Decimal("100")
    assert order.limit_price == DOGE_PRICE


def test_bearish_sell_capped_by_available_doge() -> None:
    # 500 DOGE ($50 total) but only 50 available (rest reserved in existing sell)
    existing_pending = PendingOrder(
        client_order_id="old-sell-2", kind="inventory_sell", pair="DOGE/USD",
        side=OrderSide.SELL, base_qty=Decimal("450"), quote_qty=ZERO_DECIMAL,
    )
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("500")),
        beliefs=_bearish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        pending_orders=(existing_pending,),
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    order = [a for a in actions if isinstance(a, PlaceOrder)][0].order
    # Wants 100 DOGE ($10/$0.10) but only 50 available (500-450) → capped at 50
    assert order.quantity == Decimal("50")


def test_bearish_sell_respects_reserved() -> None:
    existing_pending = PendingOrder(
        client_order_id="old-sell", kind="inventory_sell", pair="DOGE/USD",
        side=OrderSide.SELL, base_qty=Decimal("80"), quote_qty=ZERO_DECIMAL,
    )
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("100")),
        beliefs=_bearish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        pending_orders=(existing_pending,),
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    order = [a for a in actions if isinstance(a, PlaceOrder)][0].order
    # 100 DOGE - 80 reserved = 20 available; wants 100, capped at 20
    assert order.quantity == Decimal("20")


def test_bearish_sell_no_doge_returns_log() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100"), cash_doge=ZERO_DECIMAL),
        beliefs=_bearish_beliefs(),
        reference_prices=DOGE_REF_PRICES,
        as_of=NOW,
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    assert not [a for a in actions if isinstance(a, PlaceOrder)]
    assert any("no DOGE inventory" in a.message for a in actions if isinstance(a, LogEvent))


def test_bearish_sell_no_price_returns_log() -> None:
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("5000")),
        beliefs=_bearish_beliefs(),
        as_of=NOW,  # no reference_prices
    )
    event = BeliefUpdate(belief=BeliefSnapshot(
        pair="DOGE/USD", direction=BeliefDirection.BEARISH, confidence=0.9, regime=MarketRegime.TRENDING,
    ))
    _, actions = reduce(state, event, _settings())
    assert not [a for a in actions if isinstance(a, PlaceOrder)]
    assert any("no reference price" in a.message for a in actions if isinstance(a, LogEvent))


# ---------------------------------------------------------------------------
# FillConfirmed
# ---------------------------------------------------------------------------


def test_fill_confirmed_updates_portfolio() -> None:
    pending = PendingOrder(
        client_order_id="kbv4-dogeusd-000000-entry", kind="position_entry",
        pair="DOGE/USD", side=OrderSide.BUY, base_qty=Decimal("50"),
        quote_qty=Decimal("10"), position_id="kbv4-dogeusd-000000",
    )
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("100")),
        beliefs=_bullish_beliefs(),
        pending_orders=(pending,),
        open_orders=(
            OrderSnapshot(
                order_id="TXID-001", pair="DOGE/USD", side=OrderSide.BUY,
                order_type=OrderType.LIMIT, status=OrderStatus.OPEN,
                quantity=Decimal("50"), client_order_id="kbv4-dogeusd-000000-entry",
            ),
        ),
        as_of=NOW,
    )
    event = FillConfirmed(
        order_id="TXID-001", pair="DOGE/USD",
        filled_quantity=Decimal("50"), fill_price=Decimal("0.20"),
        client_order_id="kbv4-dogeusd-000000-entry",
    )
    new_state, actions = reduce(state, event, _settings())
    assert len(new_state.portfolio.positions) == 1
    assert new_state.portfolio.positions[0].quantity == Decimal("50")
    assert len(new_state.pending_orders) == 0
    assert any("fill_confirmed" in a.message for a in actions if isinstance(a, LogEvent))


def test_fill_confirmed_unknown_order_returns_log() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW)
    event = FillConfirmed(
        order_id="UNKNOWN", pair="DOGE/USD",
        filled_quantity=Decimal("50"), fill_price=Decimal("0.20"),
    )
    new_state, actions = reduce(state, event, _settings())
    assert new_state is state
    assert isinstance(actions[0], LogEvent)
    assert "no pending order" in actions[0].message


def test_inventory_sell_fill_transfers_doge_to_usd() -> None:
    pending = PendingOrder(
        client_order_id="kbv4-sell-001", kind="inventory_sell",
        pair="DOGE/USD", side=OrderSide.SELL, base_qty=Decimal("100"),
        quote_qty=ZERO_DECIMAL,
    )
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("5000")),
        pending_orders=(pending,),
        as_of=NOW,
    )
    event = FillConfirmed(
        order_id="TXID-002", pair="DOGE/USD",
        filled_quantity=Decimal("100"), fill_price=Decimal("0.10"),
        client_order_id="kbv4-sell-001",
    )
    new_state, actions = reduce(state, event, _settings())
    # DOGE reduced, USD increased
    assert new_state.portfolio.cash_doge == Decimal("4900")  # 5000 - 100
    assert new_state.portfolio.cash_usd == Decimal("10")  # 100 * 0.10
    # Pending order removed (fully filled)
    assert len(new_state.pending_orders) == 0


def test_inventory_sell_partial_fill() -> None:
    pending = PendingOrder(
        client_order_id="kbv4-sell-001", kind="inventory_sell",
        pair="DOGE/USD", side=OrderSide.SELL, base_qty=Decimal("100"),
        quote_qty=ZERO_DECIMAL,
    )
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("5000")),
        pending_orders=(pending,),
        as_of=NOW,
    )
    event = FillConfirmed(
        order_id="TXID-002", pair="DOGE/USD",
        filled_quantity=Decimal("40"), fill_price=Decimal("0.10"),
        client_order_id="kbv4-sell-001",
    )
    new_state, actions = reduce(state, event, _settings())
    assert new_state.portfolio.cash_doge == Decimal("4960")  # 5000 - 40
    assert new_state.portfolio.cash_usd == Decimal("4")  # 40 * 0.10
    # Pending retained with updated filled_qty
    assert len(new_state.pending_orders) == 1
    assert new_state.pending_orders[0].filled_qty == Decimal("40")


# ---------------------------------------------------------------------------
# ReconciliationResult
# ---------------------------------------------------------------------------


def test_reconciliation_soft_drawdown_blocks_entries() -> None:
    portfolio = Portfolio(cash_usd=Decimal("100"), total_value_usd=Decimal("100"), max_drawdown=Decimal("0.12"))
    state = BotState(portfolio=portfolio, as_of=NOW)
    event = ReconciliationResult(balances=(), open_orders=(), discrepancy_detected=False, summary="ok")
    new_state, actions = reduce(state, event, _settings())
    assert new_state.entry_blocked is True
    assert any("soft drawdown" in a.message for a in actions if isinstance(a, LogEvent))


def test_reconciliation_hard_drawdown_closes_all() -> None:
    pos = _position()
    portfolio = Portfolio(
        cash_usd=Decimal("50"), positions=(pos,),
        total_value_usd=Decimal("70"), max_drawdown=Decimal("0.20"),
    )
    state = BotState(portfolio=portfolio, as_of=NOW)
    event = ReconciliationResult(balances=(), open_orders=(), discrepancy_detected=True, summary="hard drawdown")
    new_state, actions = reduce(state, event, _settings())
    assert new_state.entry_blocked is True
    close_actions = [a for a in actions if isinstance(a, ClosePosition)]
    assert len(close_actions) == 1


def test_reconciliation_clears_entry_block_when_healthy() -> None:
    state = BotState(portfolio=_portfolio(), as_of=NOW, entry_blocked=True)
    event = ReconciliationResult(balances=(), open_orders=(), discrepancy_detected=False, summary="ok")
    new_state, _ = reduce(state, event, _settings())
    assert new_state.entry_blocked is False


def test_reconciliation_syncs_balances_and_prunes_pending() -> None:
    from core.types import Balance

    stale_pending = PendingOrder(
        client_order_id="cancelled-order", kind="inventory_sell",
        pair="DOGE/USD", side=OrderSide.SELL, base_qty=Decimal("50"),
        quote_qty=ZERO_DECIMAL,
    )
    state = BotState(
        portfolio=_portfolio(cash_usd=Decimal("0"), cash_doge=Decimal("100")),
        pending_orders=(stale_pending,),
        as_of=NOW,
    )
    event = ReconciliationResult(
        balances=(Balance(asset="DOGE", available=Decimal("150"), held=ZERO_DECIMAL),
                  Balance(asset="USD", available=Decimal("25"), held=ZERO_DECIMAL)),
        open_orders=(),  # no open orders → stale pending should be pruned
        discrepancy_detected=False, summary="ok",
    )
    new_state, _ = reduce(state, event, _settings())
    assert new_state.portfolio.cash_doge == Decimal("150")
    assert new_state.portfolio.cash_usd == Decimal("25")
    assert len(new_state.pending_orders) == 0  # pruned


# ---------------------------------------------------------------------------
# GridCycleComplete
# ---------------------------------------------------------------------------


def test_grid_cycle_complete_logs() -> None:
    state = BotState(as_of=NOW)
    event = GridCycleComplete(pair="DOGE/USD", realized_pnl_usd=Decimal("1.50"))
    new_state, actions = reduce(state, event, _settings())
    assert new_state is state
    assert isinstance(actions[0], LogEvent)
    assert "grid_cycle_complete" in actions[0].message


# ---------------------------------------------------------------------------
# DOGE as managed exposure (risk)
# ---------------------------------------------------------------------------


def test_portfolio_total_value_includes_doge() -> None:
    p = _portfolio(cash_usd=Decimal("10"), cash_doge=Decimal("1000"))
    # total = $10 + 1000 * $0.10 = $110
    assert p.total_value_usd == Decimal("110")
