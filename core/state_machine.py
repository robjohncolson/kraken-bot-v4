from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Final, TypeAlias

from beliefs.consensus import compute_consensus
from core.config import Settings
from core.types import (
    Action,
    BeliefDirection,
    BeliefSnapshot,
    BeliefUpdate,
    BotState,
    Event,
    FillConfirmed,
    GridCycleComplete,
    LogEvent,
    OrderRequest,
    OrderSide,
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
from trading.portfolio import PortfolioManager
from trading.position import PositionLifecycle
from trading.risk_rules import BlockNewEntries, CloseAllPositions, check_portfolio_rules
from trading.sizing import size_position_usd

ReducerActions: TypeAlias = tuple[Action, ...]
ReducerResult: TypeAlias = tuple[BotState, ReducerActions]

NO_ACTIONS: Final[ReducerActions] = ()

PERCENT_BASE = Decimal("100")


class UnsupportedEventError(TypeError):
    """Raised when reduce receives an event outside the declared Event union."""

    def __init__(self, event_name: str) -> None:
        self.event_name = event_name
        super().__init__(f"Unsupported reducer event: {event_name}")


def reduce(state: BotState, event: Event, config: Settings) -> ReducerResult:
    """Pure reducer: map each event to state transition + emitted actions."""

    match event:
        case PriceTick():
            return _handle_price_tick(state, event, config)
        case FillConfirmed():
            return _handle_fill_confirmed(state, event, config)
        case StopTriggered():
            return _handle_stop_triggered(state, event, config)
        case TargetHit():
            return _handle_target_hit(state, event, config)
        case WindowExpired():
            return _handle_window_expired(state, event, config)
        case BeliefUpdate():
            return _handle_belief_update(state, event, config)
        case ReconciliationResult():
            return _handle_reconciliation(state, event, config)
        case GridCycleComplete():
            return _handle_grid_cycle_complete(state, event, config)
        case _:
            raise UnsupportedEventError(type(event).__name__)


# ---------------------------------------------------------------------------
# Derived reservation helpers
# ---------------------------------------------------------------------------


def _doge_reserved(state: BotState) -> Decimal:
    """DOGE reserved in pending inventory sell orders."""
    return sum(
        (po.base_qty - po.filled_qty for po in state.pending_orders if po.kind == "inventory_sell"),
        start=ZERO_DECIMAL,
    )


def _usd_reserved(state: BotState) -> Decimal:
    """USD reserved in pending position entry orders."""
    return sum(
        (po.quote_qty for po in state.pending_orders
         if po.kind == "position_entry" and po.filled_qty < po.base_qty),
        start=ZERO_DECIMAL,
    )


def _get_reference_price(state: BotState, pair: str) -> Decimal | None:
    """Look up current price from scheduler-injected reference_prices."""
    for ref_pair, price in state.reference_prices:
        if ref_pair == pair:
            return price
    return None


# ---------------------------------------------------------------------------
# PriceTick — no-op for MVP (Guardian handles stop/target monitoring)
# ---------------------------------------------------------------------------


def _handle_price_tick(
    state: BotState, event: PriceTick, config: Settings,
) -> ReducerResult:
    _ = event, config
    return state, NO_ACTIONS


# ---------------------------------------------------------------------------
# StopTriggered — close position, record cooldown
# ---------------------------------------------------------------------------


def _handle_stop_triggered(
    state: BotState, event: StopTriggered, config: Settings,
) -> ReducerResult:
    position = _find_position(state.portfolio, event.position_id)
    if position is None:
        return state, (LogEvent(message=f"stop_triggered: position {event.position_id} not found"),)

    _, lifecycle_actions = PositionLifecycle.close_position(position, reason="stop_loss")

    doge_belief = _belief_for_pair(state.beliefs, "DOGE/USD")
    doge_price = _doge_market_price(state, event.trigger_price, position.pair)
    updated_portfolio, portfolio_actions = PortfolioManager.apply_close(
        state.portfolio,
        position_id=event.position_id,
        close_price=event.trigger_price,
        doge_belief=doge_belief,
        doge_market_price=doge_price,
    )

    cooldown_ts = state.as_of.isoformat() if state.as_of else ""
    updated_cooldowns = state.cooldowns + ((position.pair, cooldown_ts),)

    new_state = replace(
        state,
        portfolio=updated_portfolio,
        cooldowns=updated_cooldowns,
    )
    return new_state, lifecycle_actions + portfolio_actions


# ---------------------------------------------------------------------------
# TargetHit — close position (no cooldown), DOGE accumulation
# ---------------------------------------------------------------------------


def _handle_target_hit(
    state: BotState, event: TargetHit, config: Settings,
) -> ReducerResult:
    position = _find_position(state.portfolio, event.position_id)
    if position is None:
        return state, (LogEvent(message=f"target_hit: position {event.position_id} not found"),)

    _, lifecycle_actions = PositionLifecycle.close_position(position, reason="target_hit")

    doge_belief = _belief_for_pair(state.beliefs, "DOGE/USD")
    doge_price = _doge_market_price(state, event.trigger_price, position.pair)
    updated_portfolio, portfolio_actions = PortfolioManager.apply_close(
        state.portfolio,
        position_id=event.position_id,
        close_price=event.trigger_price,
        doge_belief=doge_belief,
        doge_market_price=doge_price,
    )

    new_state = replace(state, portfolio=updated_portfolio)
    return new_state, lifecycle_actions + portfolio_actions


def _handle_window_expired(
    state: BotState,
    event: WindowExpired,
    config: Settings,
) -> ReducerResult:
    position = (
        _find_position(state.portfolio, event.position_id)
        if event.position_id is not None
        else _find_position_by_pair(state.portfolio, event.pair)
    )
    if position is None:
        return state, (LogEvent(message=f"window_expired: position for {event.pair} not found"),)

    _, lifecycle_actions = PositionLifecycle.close_position(position, reason="window_expired")

    close_price = (
        event.trigger_price
        or _get_reference_price(state, position.pair)
        or position.entry_price
    )
    doge_belief = _belief_for_pair(state.beliefs, "DOGE/USD")
    doge_price = _doge_market_price(state, close_price, position.pair)
    updated_portfolio, portfolio_actions = PortfolioManager.apply_close(
        state.portfolio,
        position_id=position.position_id,
        close_price=close_price,
        doge_belief=doge_belief,
        doge_market_price=doge_price,
    )

    new_state = replace(state, portfolio=updated_portfolio)
    return new_state, lifecycle_actions + portfolio_actions


# ---------------------------------------------------------------------------
# BeliefUpdate — consensus check, buy-side entry or sell-side inventory
# ---------------------------------------------------------------------------


def _handle_belief_update(
    state: BotState, event: BeliefUpdate, config: Settings,
) -> ReducerResult:
    pair = event.belief.pair
    merged_beliefs = _upsert_belief(state.beliefs, event.belief)
    pair_beliefs = [b for b in merged_beliefs if b.pair == pair]
    consensus = compute_consensus(pair_beliefs)

    existing_position = _find_position_by_pair(state.portfolio, pair)
    expected_side = _side_for_direction(consensus.agreed_direction)

    # Consensus flipped or dissolved — close existing position
    if existing_position is not None and (
        expected_side is None or expected_side != existing_position.side
    ):
        return _close_position_on_belief_change(state, existing_position, consensus, config)

    # No directional consensus — nothing to do
    if expected_side is None:
        return state, NO_ACTIONS

    # Already have a position in the right direction
    if existing_position is not None:
        return state, NO_ACTIONS

    # --- Entry logic ---
    if state.entry_blocked:
        return state, (LogEvent(message=f"belief_update: entry blocked for {pair}"),)

    # Portfolio risk check
    risk_result = check_portfolio_rules(state.portfolio, config=config)
    if isinstance(risk_result.recommended_action, CloseAllPositions):
        return state, risk_result.recommended_action.actions
    if isinstance(risk_result.recommended_action, BlockNewEntries):
        return replace(state, entry_blocked=True), (
            LogEvent(message=f"belief_update: soft drawdown blocks entry for {pair}"),
        )

    # Cooldown check
    if _pair_in_cooldown(state, pair, config):
        return state, (LogEvent(message=f"belief_update: {pair} in re-entry cooldown"),)

    # Reference price required for base-asset conversion
    ref_price = _get_reference_price(state, pair)
    if ref_price is None or ref_price <= ZERO_DECIMAL:
        return state, (LogEvent(message=f"belief_update: no reference price for {pair}"),)

    # Bearish DOGE sell: doesn't need USD funds — sells existing inventory
    if expected_side == PositionSide.SHORT and pair == "DOGE/USD":
        sell_usd = Decimal(config.min_position_usd)
        return _bearish_inventory_sell(state, pair, sell_usd, ref_price, config)

    # Position sizing (buy-side entries)
    kelly_frac = ZERO_DECIMAL  # safe default until stats engine has enough samples
    min_usd = Decimal(config.min_position_usd)
    max_usd = Decimal(config.max_position_usd)
    position_usd = size_position_usd(
        portfolio_value_usd=state.portfolio.total_value_usd,
        kelly_fraction_value=kelly_frac,
        min_position_usd=min_usd,
        max_position_usd=max_usd,
    )
    if position_usd <= ZERO_DECIMAL:
        if state.portfolio.total_value_usd >= min_usd:
            position_usd = min_usd
        else:
            return state, (LogEvent(message=f"belief_update: insufficient funds for {pair}"),)

    return _bullish_position_entry(state, pair, position_usd, ref_price, expected_side, config)


def _bullish_position_entry(
    state: BotState, pair: str, position_usd: Decimal,
    ref_price: Decimal, expected_side: PositionSide, config: Settings,
) -> ReducerResult:
    """Buy-side entry: cap by free USD, quantity in base-asset."""
    _ = config
    available_usd = state.portfolio.cash_usd - _usd_reserved(state)
    capped_usd = min(position_usd, available_usd)
    min_usd = Decimal(config.min_position_usd)
    if capped_usd < min_usd:
        return state, (LogEvent(message=f"belief_update: insufficient free USD for {pair} (have ${available_usd}, need ${min_usd})"),)

    base_qty = capped_usd / ref_price  # convert USD to base-asset quantity

    seq = state.next_position_seq
    pair_slug = pair.replace("/", "").lower()
    position_id = f"kbv4-{pair_slug}-{seq:06d}"
    client_order_id = f"kbv4-{pair_slug}-{seq:06d}-entry"

    order = OrderRequest(
        pair=pair,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=base_qty,
        limit_price=ref_price,
        client_order_id=client_order_id,
    )
    pending = PendingOrder(
        client_order_id=client_order_id,
        kind="position_entry",
        pair=pair,
        side=OrderSide.BUY,
        base_qty=base_qty,
        quote_qty=capped_usd,
        position_id=position_id,
    )
    new_state = replace(
        state,
        next_position_seq=seq + 1,
        pending_orders=state.pending_orders + (pending,),
    )
    return new_state, (
        PlaceOrder(order=order),
        LogEvent(message=f"belief_update: BUY {pair} qty={base_qty:.4f} @ {ref_price} (${capped_usd})"),
    )


def _bearish_inventory_sell(
    state: BotState, pair: str, position_usd: Decimal,
    ref_price: Decimal, config: Settings,
) -> ReducerResult:
    """Spot inventory sell: reduce DOGE exposure on bearish consensus. No Position created."""
    _ = config
    available_doge = state.portfolio.cash_doge - _doge_reserved(state)
    if available_doge <= ZERO_DECIMAL:
        return state, (LogEvent(message="belief_update: no DOGE inventory to sell"),)

    sell_qty = position_usd / ref_price
    sell_qty = min(sell_qty, available_doge)  # cap by available balance

    if sell_qty <= ZERO_DECIMAL:
        return state, (LogEvent(message=f"belief_update: sell quantity too small for {pair}"),)

    seq = state.next_position_seq
    pair_slug = pair.replace("/", "").lower()
    client_order_id = f"kbv4-{pair_slug}-{seq:06d}-sell"

    order = OrderRequest(
        pair=pair,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=sell_qty,
        limit_price=ref_price,
        client_order_id=client_order_id,
    )
    pending = PendingOrder(
        client_order_id=client_order_id,
        kind="inventory_sell",
        pair=pair,
        side=OrderSide.SELL,
        base_qty=sell_qty,
        quote_qty=ZERO_DECIMAL,
    )
    new_state = replace(
        state,
        next_position_seq=seq + 1,
        pending_orders=state.pending_orders + (pending,),
    )
    return new_state, (
        PlaceOrder(order=order),
        LogEvent(message=f"belief_update: SELL {pair} qty={sell_qty:.4f} @ {ref_price} (inventory)"),
    )


# ---------------------------------------------------------------------------
# FillConfirmed — route to inventory accounting or position entry
# ---------------------------------------------------------------------------


def _handle_fill_confirmed(
    state: BotState, event: FillConfirmed, config: Settings,
) -> ReducerResult:
    pending = _match_fill_to_pending(state, event)
    if pending is None:
        return state, (
            LogEvent(message=f"fill_confirmed: no pending order for {event.order_id}"),
        )

    if pending.kind == "inventory_sell":
        return _handle_inventory_sell_fill(state, event, pending)
    return _handle_position_entry_fill(state, event, pending, config)


def _handle_inventory_sell_fill(
    state: BotState, event: FillConfirmed, pending: PendingOrder,
) -> ReducerResult:
    """DOGE→USD inventory transfer on fill."""
    received_usd = event.filled_quantity * event.fill_price
    new_portfolio = replace(
        state.portfolio,
        cash_doge=state.portfolio.cash_doge - event.filled_quantity,
        cash_usd=state.portfolio.cash_usd + received_usd,
    )

    updated_pending = replace(pending, filled_qty=pending.filled_qty + event.filled_quantity)

    if updated_pending.filled_qty >= updated_pending.base_qty:
        remaining = tuple(po for po in state.pending_orders if po.client_order_id != pending.client_order_id)
    else:
        remaining = tuple(
            updated_pending if po.client_order_id == pending.client_order_id else po
            for po in state.pending_orders
        )

    new_state = replace(state, portfolio=new_portfolio, pending_orders=remaining)
    return new_state, (
        LogEvent(message=f"fill_confirmed: sold {event.filled_quantity} {event.pair} @ {event.fill_price} → ${received_usd:.2f}"),
    )


def _handle_position_entry_fill(
    state: BotState, event: FillConfirmed, pending: PendingOrder, config: Settings,
) -> ReducerResult:
    """Position entry fill — create Position with stop/target."""
    position_id = pending.position_id or f"kbv4-fill-{event.order_id}"
    side = PositionSide.LONG if pending.side == OrderSide.BUY else PositionSide.SHORT

    position = Position(
        position_id=position_id,
        pair=event.pair,
        side=side,
        quantity=event.filled_quantity,
        entry_price=event.fill_price,
        stop_price=ZERO_DECIMAL,
        target_price=ZERO_DECIMAL,
    )

    belief = _belief_for_pair(state.beliefs, event.pair)
    if belief is not None and belief.direction != BeliefDirection.NEUTRAL:
        position, lifecycle_actions = PositionLifecycle.open_position(
            position, belief=belief, config=config,
        )
    else:
        lifecycle_actions = NO_ACTIONS

    updated_portfolio = PortfolioManager.apply_fill(state.portfolio, position=position)

    updated_pending = replace(pending, filled_qty=pending.filled_qty + event.filled_quantity)
    if updated_pending.filled_qty >= updated_pending.base_qty:
        remaining = tuple(po for po in state.pending_orders if po.client_order_id != pending.client_order_id)
    else:
        remaining = tuple(
            updated_pending if po.client_order_id == pending.client_order_id else po
            for po in state.pending_orders
        )

    new_state = replace(state, portfolio=updated_portfolio, pending_orders=remaining)
    return new_state, lifecycle_actions + (
        LogEvent(message=f"fill_confirmed: {event.pair} qty={event.filled_quantity} @ {event.fill_price}"),
    )


# ---------------------------------------------------------------------------
# ReconciliationResult — sync balances, prune stale pending, check risk
# ---------------------------------------------------------------------------


def _handle_reconciliation(
    state: BotState, event: ReconciliationResult, config: Settings,
) -> ReducerResult:
    # Sync balances from exchange truth
    actual_doge = sum(
        (b.available + b.held for b in event.balances if b.asset == "DOGE"),
        start=ZERO_DECIMAL,
    )
    actual_usd = sum(
        (b.available + b.held for b in event.balances if b.asset == "USD"),
        start=ZERO_DECIMAL,
    )
    synced_portfolio = replace(
        state.portfolio, cash_doge=actual_doge, cash_usd=actual_usd,
    )

    # Prune pending orders not in exchange open orders
    exchange_client_oids = {
        o.client_order_id for o in event.open_orders if o.client_order_id
    }
    live_pending = tuple(
        po for po in state.pending_orders if po.client_order_id in exchange_client_oids
    )

    synced_state = replace(
        state, portfolio=synced_portfolio, pending_orders=live_pending,
    )

    # Risk check
    risk_result = check_portfolio_rules(synced_state.portfolio, config=config)

    if isinstance(risk_result.recommended_action, CloseAllPositions):
        new_state = replace(synced_state, entry_blocked=True)
        return new_state, risk_result.recommended_action.actions + (
            LogEvent(message="reconciliation: hard drawdown — closing all positions"),
        )

    if isinstance(risk_result.recommended_action, BlockNewEntries):
        new_state = replace(synced_state, entry_blocked=True)
        return new_state, (
            LogEvent(message="reconciliation: soft drawdown — blocking new entries"),
        )

    new_state = replace(synced_state, entry_blocked=False)
    return new_state, (
        LogEvent(message=f"reconciliation: {event.summary or 'ok'}"),
    )


# ---------------------------------------------------------------------------
# GridCycleComplete — no-op for MVP (grid deferred)
# ---------------------------------------------------------------------------


def _handle_grid_cycle_complete(
    state: BotState, event: GridCycleComplete, config: Settings,
) -> ReducerResult:
    _ = config
    return state, (
        LogEvent(message=f"grid_cycle_complete: {event.pair} pnl=${event.realized_pnl_usd}"),
    )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _find_position(portfolio: Portfolio, position_id: str) -> Position | None:
    for p in portfolio.positions:
        if p.position_id == position_id:
            return p
    return None


def _find_position_by_pair(portfolio: Portfolio, pair: str) -> Position | None:
    for p in portfolio.positions:
        if p.pair == pair:
            return p
    return None


def _belief_for_pair(
    beliefs: tuple[BeliefSnapshot, ...], pair: str,
) -> BeliefSnapshot | None:
    for b in beliefs:
        if b.pair == pair:
            return b
    return None


def _upsert_belief(
    beliefs: tuple[BeliefSnapshot, ...], new_belief: BeliefSnapshot,
) -> tuple[BeliefSnapshot, ...]:
    updated = [b for b in beliefs if b.pair != new_belief.pair]
    updated.append(new_belief)
    return tuple(sorted(updated, key=lambda b: b.pair))


def _side_for_direction(direction: BeliefDirection) -> PositionSide | None:
    if direction == BeliefDirection.BULLISH:
        return PositionSide.LONG
    if direction == BeliefDirection.BEARISH:
        return PositionSide.SHORT
    return None


def _close_position_on_belief_change(
    state: BotState,
    position: Position,
    consensus: object,
    config: Settings,
) -> ReducerResult:
    _ = consensus, config
    _, lifecycle_actions = PositionLifecycle.close_position(
        position, reason="belief_change",
    )
    doge_belief = _belief_for_pair(state.beliefs, "DOGE/USD")
    updated_portfolio, portfolio_actions = PortfolioManager.apply_close(
        state.portfolio,
        position_id=position.position_id,
        close_price=position.entry_price,
        doge_belief=doge_belief,
        doge_market_price=position.entry_price if position.pair == "DOGE/USD" else None,
    )
    new_state = replace(state, portfolio=updated_portfolio)
    return new_state, lifecycle_actions + portfolio_actions


def _pair_in_cooldown(state: BotState, pair: str, config: Settings) -> bool:
    if state.as_of is None:
        return False
    cooldown_hours = config.reentry_cooldown_hours
    for cooldown_pair, cooldown_ts in state.cooldowns:
        if cooldown_pair != pair:
            continue
        if not cooldown_ts:
            continue
        try:
            stopped_at = datetime.fromisoformat(cooldown_ts)
        except (ValueError, TypeError):
            continue
        if stopped_at.tzinfo is None:
            stopped_at = stopped_at.replace(tzinfo=timezone.utc)
        as_of = state.as_of
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        if as_of < stopped_at + timedelta(hours=cooldown_hours):
            return True
    return False


def _match_fill_to_pending(state: BotState, event: FillConfirmed) -> PendingOrder | None:
    """Match a fill to a PendingOrder, preferring client_order_id."""
    # Direct match by client_order_id (most reliable)
    if event.client_order_id:
        for po in state.pending_orders:
            if po.client_order_id == event.client_order_id:
                return po

    # Fallback: match via open_orders order_id → client_order_id → pending
    for order in state.open_orders:
        if order.order_id == event.order_id and order.client_order_id:
            for po in state.pending_orders:
                if po.client_order_id == order.client_order_id:
                    return po

    # Last resort: single pending order for this pair
    pair_matches = [po for po in state.pending_orders if po.pair == event.pair]
    if len(pair_matches) == 1:
        return pair_matches[0]

    return None


def _doge_market_price(
    state: BotState, trigger_price: Decimal, position_pair: str,
) -> Decimal | None:
    """Return DOGE market price for accumulation, if applicable."""
    if position_pair == "DOGE/USD":
        return trigger_price
    price = _get_reference_price(state, "DOGE/USD")
    return price


__all__ = [
    "NO_ACTIONS",
    "ReducerActions",
    "ReducerResult",
    "UnsupportedEventError",
    "reduce",
]
