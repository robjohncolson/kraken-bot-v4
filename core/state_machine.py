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
    PlaceOrder,
    Portfolio,
    Position,
    PositionSide,
    PriceTick,
    ReconciliationResult,
    StopTriggered,
    TargetHit,
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
        case BeliefUpdate():
            return _handle_belief_update(state, event, config)
        case ReconciliationResult():
            return _handle_reconciliation(state, event, config)
        case GridCycleComplete():
            return _handle_grid_cycle_complete(state, event, config)
        case _:
            raise UnsupportedEventError(type(event).__name__)


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


# ---------------------------------------------------------------------------
# BeliefUpdate — consensus check, open/close positions
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

    # Max positions check
    if len(state.portfolio.positions) >= config.max_positions:
        return state, (LogEvent(message=f"belief_update: max positions ({config.max_positions}) reached"),)

    # Cooldown check
    if _pair_in_cooldown(state, pair, config):
        return state, (LogEvent(message=f"belief_update: {pair} in re-entry cooldown"),)

    # Position sizing — with no trade history Kelly returns 0, use min as fallback
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
        # No Kelly edge yet — fall back to minimum position size if portfolio can cover it
        if state.portfolio.total_value_usd >= min_usd:
            position_usd = min_usd
        else:
            return state, (LogEvent(message=f"belief_update: insufficient funds for {pair}"),)

    # Build entry
    seq = state.next_position_seq
    pair_slug = pair.replace("/", "").lower()
    position_id = f"kbv4-{pair_slug}-{seq:06d}"
    client_order_id = f"kbv4-{pair_slug}-{seq:06d}-entry"

    # Entry price: use the belief's confidence-weighted price placeholder.
    # In practice the limit order goes at a maker offset below/above market.
    # For now we use a placeholder; the runtime will have current market price.
    # The reducer emits the PlaceOrder; the OrderGate handles price validation.
    entry_quantity = position_usd  # quantity in USD terms for the order

    order_side = OrderSide.BUY if expected_side == PositionSide.LONG else OrderSide.SELL
    order = OrderRequest(
        pair=pair,
        side=order_side,
        order_type=OrderType.LIMIT,
        quantity=entry_quantity,
        client_order_id=client_order_id,
    )

    updated_pending = state.pending_orders + ((client_order_id, position_id),)
    new_state = replace(
        state,
        next_position_seq=seq + 1,
        pending_orders=updated_pending,
    )
    return new_state, (
        PlaceOrder(order=order),
        LogEvent(message=f"belief_update: entry order for {pair} side={expected_side.value} size=${position_usd}"),
    )


# ---------------------------------------------------------------------------
# FillConfirmed — update portfolio with fill data
# ---------------------------------------------------------------------------


def _handle_fill_confirmed(
    state: BotState, event: FillConfirmed, config: Settings,
) -> ReducerResult:
    # Try to match via pending_orders first (by scanning open_orders for the fill's order_id)
    matched_position_id = _match_fill_to_pending(state, event)
    if matched_position_id is None:
        return state, (
            LogEvent(message=f"fill_confirmed: no pending order for order_id={event.order_id}"),
        )

    side = _infer_side_from_order(state, event)

    # Build/update the position from fill data
    position = Position(
        position_id=matched_position_id,
        pair=event.pair,
        side=side,
        quantity=event.filled_quantity,
        entry_price=event.fill_price,
        stop_price=ZERO_DECIMAL,
        target_price=ZERO_DECIMAL,
    )

    # Open the position with stop/target via lifecycle
    belief = _belief_for_pair(state.beliefs, event.pair)
    if belief is not None and belief.direction != BeliefDirection.NEUTRAL:
        position, lifecycle_actions = PositionLifecycle.open_position(
            position, belief=belief, config=config,
        )
    else:
        lifecycle_actions = NO_ACTIONS

    updated_portfolio = PortfolioManager.apply_fill(state.portfolio, position=position)

    # Remove matched pending order
    remaining_pending = tuple(
        po for po in state.pending_orders if po[1] != matched_position_id
    )

    new_state = replace(
        state,
        portfolio=updated_portfolio,
        pending_orders=remaining_pending,
    )
    return new_state, lifecycle_actions + (
        LogEvent(message=f"fill_confirmed: {event.pair} qty={event.filled_quantity} @ {event.fill_price}"),
    )


# ---------------------------------------------------------------------------
# ReconciliationResult — check risk, block entries or close all
# ---------------------------------------------------------------------------


def _handle_reconciliation(
    state: BotState, event: ReconciliationResult, config: Settings,
) -> ReducerResult:
    risk_result = check_portfolio_rules(state.portfolio, config=config)

    if isinstance(risk_result.recommended_action, CloseAllPositions):
        new_state = replace(state, entry_blocked=True)
        return new_state, risk_result.recommended_action.actions + (
            LogEvent(message="reconciliation: hard drawdown — closing all positions"),
        )

    if isinstance(risk_result.recommended_action, BlockNewEntries):
        new_state = replace(state, entry_blocked=True)
        return new_state, (
            LogEvent(message="reconciliation: soft drawdown — blocking new entries"),
        )

    new_state = replace(state, entry_blocked=False)
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
    # Use entry_price as close proxy (actual close price comes from the fill)
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


def _match_fill_to_pending(state: BotState, event: FillConfirmed) -> str | None:
    """Match a fill to a pending position_id via open_orders client_order_id."""
    # Check if the order_id matches any open order with a client_order_id in pending_orders
    for order in state.open_orders:
        if order.order_id == event.order_id and order.client_order_id:
            for client_oid, position_id in state.pending_orders:
                if client_oid == order.client_order_id:
                    return position_id

    # Fallback: match by pair if only one pending order for that pair
    pair_matches = [
        position_id
        for _, position_id in state.pending_orders
        if position_id.replace("kbv4-", "").rsplit("-", 1)[0].replace("usd", "/usd").replace("doge", "doge") == event.pair.lower().replace("/", "")
    ]
    # Simple pair-slug match
    pair_slug = event.pair.replace("/", "").lower()
    pair_matches = [
        position_id
        for client_oid, position_id in state.pending_orders
        if pair_slug in position_id
    ]
    if len(pair_matches) == 1:
        return pair_matches[0]

    return None


def _infer_side_from_order(state: BotState, event: FillConfirmed) -> PositionSide:
    """Infer position side from the order that was filled."""
    for order in state.open_orders:
        if order.order_id == event.order_id:
            if order.side == OrderSide.BUY:
                return PositionSide.LONG
            return PositionSide.SHORT
    # Default to long for buys
    return PositionSide.LONG


def _doge_market_price(
    state: BotState, trigger_price: Decimal, position_pair: str,
) -> Decimal | None:
    """Return DOGE market price for accumulation, if applicable."""
    if position_pair == "DOGE/USD":
        return trigger_price
    # For non-DOGE pairs, we'd need the DOGE price from state
    # For MVP with DOGE-only account, this is sufficient
    return None


__all__ = [
    "NO_ACTIONS",
    "ReducerActions",
    "ReducerResult",
    "UnsupportedEventError",
    "reduce",
]
