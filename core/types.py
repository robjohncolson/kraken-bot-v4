from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias


Pair: TypeAlias = str
AssetSymbol: TypeAlias = str
PositionId: TypeAlias = str
OrderId: TypeAlias = str
ClientOrderId: TypeAlias = str
Price: TypeAlias = Decimal
Quantity: TypeAlias = Decimal
UsdAmount: TypeAlias = Decimal

ZERO_DECIMAL = Decimal("0")


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class BeliefDirection(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class BeliefSource(StrEnum):
    CLAUDE = "claude"
    CODEX = "codex"
    TECHNICAL_ENSEMBLE = "technical_ensemble"
    RESEARCH_MODEL = "research_model"


class MarketRegime(StrEnum):
    TRENDING = "trending"
    RANGING = "ranging"
    UNKNOWN = "unknown"


class GridPhase(StrEnum):
    S0 = "s0"
    S1A = "s1a"
    S1B = "s1b"
    S2 = "s2"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"


class OrderStatus(StrEnum):
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"


class CircuitBreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class EventType(StrEnum):
    PRICE_TICK = "price_tick"
    FILL_CONFIRMED = "fill_confirmed"
    STOP_TRIGGERED = "stop_triggered"
    TARGET_HIT = "target_hit"
    WINDOW_EXPIRED = "window_expired"
    BELIEF_UPDATE = "belief_update"
    RECONCILIATION_RESULT = "reconciliation_result"
    GRID_CYCLE_COMPLETE = "grid_cycle_complete"


class ActionType(StrEnum):
    PLACE_ORDER = "place_order"
    CANCEL_ORDER = "cancel_order"
    UPDATE_STOP = "update_stop"
    UPDATE_TARGET = "update_target"
    CLOSE_POSITION = "close_position"
    ACTIVATE_GRID = "activate_grid"
    DEACTIVATE_GRID = "deactivate_grid"
    REDISTRIBUTE_GRID_PROFITS = "redistribute_grid_profits"
    LOG_EVENT = "log_event"


@dataclass(frozen=True, slots=True)
class GridState:
    phase: GridPhase = GridPhase.S0
    active_slot_count: int = 0
    accepting_new_entries: bool = False
    realized_pnl_usd: UsdAmount = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class Position:
    position_id: PositionId
    pair: Pair
    side: PositionSide
    quantity: Quantity
    entry_price: Price
    stop_price: Price
    target_price: Price
    grid_state: GridState | None = None


@dataclass(frozen=True, slots=True)
class PairAllocation:
    pair: Pair
    percent: Decimal


@dataclass(frozen=True, slots=True)
class Portfolio:
    cash_usd: UsdAmount = ZERO_DECIMAL
    cash_doge: Quantity = ZERO_DECIMAL
    positions: tuple[Position, ...] = field(default_factory=tuple)
    total_value_usd: UsdAmount = ZERO_DECIMAL
    concentration: tuple[PairAllocation, ...] = field(default_factory=tuple)
    directional_exposure: Decimal = ZERO_DECIMAL
    max_drawdown: Decimal = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class Balance:
    asset: AssetSymbol
    available: Quantity
    held: Quantity = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class OrderRequest:
    pair: Pair
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    limit_price: Price | None = None
    stop_price: Price | None = None
    client_order_id: ClientOrderId | None = None


@dataclass(frozen=True, slots=True)
class OrderSnapshot:
    order_id: OrderId
    pair: Pair
    side: OrderSide
    order_type: OrderType
    status: OrderStatus
    quantity: Quantity
    filled_quantity: Quantity = ZERO_DECIMAL
    limit_price: Price | None = None
    stop_price: Price | None = None
    client_order_id: ClientOrderId | None = None


@dataclass(frozen=True, slots=True)
class BeliefSnapshot:
    pair: Pair
    direction: BeliefDirection
    confidence: float
    regime: MarketRegime = MarketRegime.UNKNOWN
    sources: tuple[BeliefSource, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DurationEstimate:
    estimated_bear_hours: int
    confidence: float
    macd_bearish: bool
    rsi_bearish: bool
    ema_bearish: bool


@dataclass(frozen=True, slots=True)
class BullCandidate:
    pair: Pair
    belief: BeliefSnapshot
    confidence: float
    reference_price_hint: Price
    estimated_peak_hours: int


@dataclass(frozen=True, slots=True)
class BotState:
    portfolio: Portfolio = field(default_factory=Portfolio)
    balances: tuple[Balance, ...] = field(default_factory=tuple)
    open_orders: tuple[OrderSnapshot, ...] = field(default_factory=tuple)
    beliefs: tuple[BeliefSnapshot, ...] = field(default_factory=tuple)
    last_event: EventType | None = None
    as_of: datetime | None = None
    next_position_seq: int = 0
    pending_orders: tuple[PendingOrder, ...] = field(default_factory=tuple)
    reference_prices: tuple[tuple[str, Decimal], ...] = field(default_factory=tuple)
    cooldowns: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    entry_blocked: bool = False


@dataclass(frozen=True, slots=True)
class PriceTick:
    pair: Pair
    price: Price
    kind: EventType = field(default=EventType.PRICE_TICK, init=False)


@dataclass(frozen=True, slots=True)
class FillConfirmed:
    order_id: OrderId
    pair: Pair
    filled_quantity: Quantity
    fill_price: Price
    client_order_id: ClientOrderId | None = None
    kind: EventType = field(default=EventType.FILL_CONFIRMED, init=False)


@dataclass(frozen=True, slots=True)
class PendingOrder:
    """Tracks an in-flight order for reservation accounting."""

    client_order_id: ClientOrderId
    kind: str  # "position_entry" | "inventory_sell"
    pair: Pair
    side: OrderSide
    base_qty: Quantity  # base-asset quantity (e.g. DOGE for DOGE/USD)
    quote_qty: UsdAmount  # quote-asset committed (USD for buys, ZERO for sells)
    filled_qty: Quantity = ZERO_DECIMAL  # base-asset filled so far
    position_id: PositionId | None = None  # set for position entries


@dataclass(frozen=True, slots=True)
class StopTriggered:
    position_id: PositionId
    trigger_price: Price
    kind: EventType = field(default=EventType.STOP_TRIGGERED, init=False)


@dataclass(frozen=True, slots=True)
class TargetHit:
    position_id: PositionId
    trigger_price: Price
    kind: EventType = field(default=EventType.TARGET_HIT, init=False)


@dataclass(frozen=True, slots=True)
class WindowExpired:
    pair: Pair
    position_id: PositionId | None = None
    trigger_price: Price | None = None
    expired_at: datetime | None = None
    kind: EventType = field(default=EventType.WINDOW_EXPIRED, init=False)


@dataclass(frozen=True, slots=True)
class BeliefUpdate:
    belief: BeliefSnapshot
    kind: EventType = field(default=EventType.BELIEF_UPDATE, init=False)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    balances: tuple[Balance, ...]
    open_orders: tuple[OrderSnapshot, ...]
    discrepancy_detected: bool
    summary: str = ""
    kind: EventType = field(default=EventType.RECONCILIATION_RESULT, init=False)


@dataclass(frozen=True, slots=True)
class GridCycleComplete:
    pair: Pair
    realized_pnl_usd: UsdAmount
    kind: EventType = field(default=EventType.GRID_CYCLE_COMPLETE, init=False)


Event: TypeAlias = (
    PriceTick
    | FillConfirmed
    | StopTriggered
    | TargetHit
    | WindowExpired
    | BeliefUpdate
    | ReconciliationResult
    | GridCycleComplete
)


@dataclass(frozen=True, slots=True)
class PlaceOrder:
    order: OrderRequest
    kind: ActionType = field(default=ActionType.PLACE_ORDER, init=False)


@dataclass(frozen=True, slots=True)
class CancelOrder:
    order_id: OrderId | None = None
    client_order_id: ClientOrderId | None = None
    kind: ActionType = field(default=ActionType.CANCEL_ORDER, init=False)


@dataclass(frozen=True, slots=True)
class UpdateStop:
    position_id: PositionId
    stop_price: Price
    kind: ActionType = field(default=ActionType.UPDATE_STOP, init=False)


@dataclass(frozen=True, slots=True)
class UpdateTarget:
    position_id: PositionId
    target_price: Price
    kind: ActionType = field(default=ActionType.UPDATE_TARGET, init=False)


@dataclass(frozen=True, slots=True)
class ClosePosition:
    position_id: PositionId
    reason: str
    kind: ActionType = field(default=ActionType.CLOSE_POSITION, init=False)


@dataclass(frozen=True, slots=True)
class ActivateGrid:
    pair: Pair
    kind: ActionType = field(default=ActionType.ACTIVATE_GRID, init=False)


@dataclass(frozen=True, slots=True)
class DeactivateGrid:
    pair: Pair
    kind: ActionType = field(default=ActionType.DEACTIVATE_GRID, init=False)


@dataclass(frozen=True, slots=True)
class RedistributeGridProfits:
    pair: Pair
    amount_usd: UsdAmount
    kind: ActionType = field(default=ActionType.REDISTRIBUTE_GRID_PROFITS, init=False)


@dataclass(frozen=True, slots=True)
class LogEvent:
    message: str
    kind: ActionType = field(default=ActionType.LOG_EVENT, init=False)


Action: TypeAlias = (
    PlaceOrder
    | CancelOrder
    | UpdateStop
    | UpdateTarget
    | ClosePosition
    | ActivateGrid
    | DeactivateGrid
    | RedistributeGridProfits
    | LogEvent
)


__all__ = [
    "Action",
    "ActionType",
    "ActivateGrid",
    "AssetSymbol",
    "Balance",
    "BeliefDirection",
    "BeliefSnapshot",
    "BeliefSource",
    "BeliefUpdate",
    "BotState",
    "CancelOrder",
    "CircuitBreakerState",
    "ClientOrderId",
    "ClosePosition",
    "DeactivateGrid",
    "Event",
    "EventType",
    "FillConfirmed",
    "GridCycleComplete",
    "GridPhase",
    "GridState",
    "LogEvent",
    "MarketRegime",
    "OrderId",
    "OrderRequest",
    "OrderSide",
    "OrderSnapshot",
    "OrderStatus",
    "OrderType",
    "Pair",
    "PairAllocation",
    "PendingOrder",
    "PlaceOrder",
    "Portfolio",
    "Position",
    "PositionId",
    "PositionSide",
    "Price",
    "PriceTick",
    "Quantity",
    "ReconciliationResult",
    "RedistributeGridProfits",
    "StopTriggered",
    "TargetHit",
    "UpdateStop",
    "UpdateTarget",
    "UsdAmount",
]
