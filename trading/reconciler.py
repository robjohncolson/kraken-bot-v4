from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from core.types import Balance, ClientOrderId, OrderId, Pair, PositionId, ZERO_DECIMAL
from exchange.models import KrakenOrder, KrakenState, KrakenTrade

DEFAULT_CLIENT_ORDER_ID_PREFIX = "kbv4"
DEFAULT_RECENT_FILL_WINDOW = timedelta(minutes=15)
DEFAULT_STALE_ORDER_AGE = timedelta(minutes=30)
DEFAULT_FEE_DRIFT_TOLERANCE = Decimal("0.01")
DEFAULT_HIGH_FEE_DRIFT_MULTIPLIER = Decimal("5")
EPOCH = datetime(1970, 1, 1)


class ReconciliationSeverity(StrEnum):
    LOW = "low"
    HIGH = "high"


class ReconciliationAction(StrEnum):
    AUTO_FIX = "auto_fix"
    ALERT = "alert"


class ForeignOrderClassification(StrEnum):
    NEW = "new"
    ACKED = "acked"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class SupabasePosition:
    position_id: PositionId
    pair: Pair


@dataclass(frozen=True, slots=True)
class SupabaseOrder:
    order_id: str
    pair: Pair
    position_id: PositionId | None = None
    exchange_order_id: OrderId | None = None
    client_order_id: ClientOrderId | None = None
    recorded_fee: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SupabaseState:
    positions: tuple[SupabasePosition, ...] = field(default_factory=tuple)
    orders: tuple[SupabaseOrder, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class GhostPosition:
    position_id: PositionId
    pair: Pair
    severity: ReconciliationSeverity
    recommended_action: ReconciliationAction
    recommended_step: str = "close_supabase_position"


@dataclass(frozen=True, slots=True)
class ForeignOrder:
    order_id: OrderId
    pair: Pair
    client_order_id: ClientOrderId | None
    classification: ForeignOrderClassification
    age: timedelta
    severity: ReconciliationSeverity
    recommended_action: ReconciliationAction
    recommended_step: str


@dataclass(frozen=True, slots=True)
class FeeDrift:
    order_id: str
    pair: Pair
    kraken_fee: Decimal
    supabase_fee: Decimal
    delta: Decimal
    severity: ReconciliationSeverity
    recommended_action: ReconciliationAction
    recommended_step: str


@dataclass(frozen=True, slots=True)
class UntrackedAsset:
    asset: str
    available: Decimal
    held: Decimal
    severity: ReconciliationSeverity
    recommended_action: ReconciliationAction
    recommended_step: str = "import_asset_balance"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    ghost_positions: tuple[GhostPosition, ...] = field(default_factory=tuple)
    foreign_orders: tuple[ForeignOrder, ...] = field(default_factory=tuple)
    fee_drift: tuple[FeeDrift, ...] = field(default_factory=tuple)
    untracked_assets: tuple[UntrackedAsset, ...] = field(default_factory=tuple)

    @property
    def discrepancy_detected(self) -> bool:
        return any(
            (
                self.ghost_positions,
                self.foreign_orders,
                self.fee_drift,
                self.untracked_assets,
            )
        )


def reconcile(
    kraken_state: KrakenState,
    supabase_state: SupabaseState,
    *,
    cl_ord_id_prefix: str = DEFAULT_CLIENT_ORDER_ID_PREFIX,
    recent_fill_window: timedelta = DEFAULT_RECENT_FILL_WINDOW,
    stale_order_age: timedelta = DEFAULT_STALE_ORDER_AGE,
    fee_drift_tolerance: Decimal = DEFAULT_FEE_DRIFT_TOLERANCE,
    high_fee_drift_tolerance: Decimal | None = None,
    as_of: datetime | None = None,
) -> ReconciliationReport:
    effective_as_of = _resolve_as_of(kraken_state, as_of=as_of)
    high_fee_limit = (
        fee_drift_tolerance * DEFAULT_HIGH_FEE_DRIFT_MULTIPLIER
        if high_fee_drift_tolerance is None
        else high_fee_drift_tolerance
    )
    return ReconciliationReport(
        ghost_positions=_detect_ghost_positions(
            kraken_state,
            supabase_state,
            as_of=effective_as_of,
            recent_fill_window=recent_fill_window,
            cl_ord_id_prefix=cl_ord_id_prefix,
        ),
        foreign_orders=_detect_foreign_orders(
            kraken_state,
            supabase_state,
            as_of=effective_as_of,
            stale_order_age=stale_order_age,
            cl_ord_id_prefix=cl_ord_id_prefix,
        ),
        fee_drift=_detect_fee_drift(
            kraken_state,
            supabase_state,
            fee_drift_tolerance=fee_drift_tolerance,
            high_fee_drift_tolerance=high_fee_limit,
        ),
        untracked_assets=_detect_untracked_assets(kraken_state, supabase_state),
    )


def _detect_ghost_positions(
    kraken_state: KrakenState,
    supabase_state: SupabaseState,
    *,
    as_of: datetime,
    recent_fill_window: timedelta,
    cl_ord_id_prefix: str,
) -> tuple[GhostPosition, ...]:
    ghost_positions: list[GhostPosition] = []
    for position in sorted(supabase_state.positions, key=lambda item: item.position_id):
        related_orders = tuple(
            order
            for order in supabase_state.orders
            if _position_id(order) == position.position_id
        )
        exchange_ids = {_exchange_order_id(order) for order in related_orders if _exchange_order_id(order)}
        client_ids = {_client_order_id(order) for order in related_orders if _client_order_id(order)}
        has_open_order = any(
            _matches_position_order(
                position,
                order,
                exchange_ids=exchange_ids,
                client_ids=client_ids,
                cl_ord_id_prefix=cl_ord_id_prefix,
            )
            for order in kraken_state.open_orders
        )
        has_recent_fill = any(
            _is_recent_fill(
                position,
                trade,
                exchange_ids=exchange_ids,
                client_ids=client_ids,
                window_start=as_of - recent_fill_window,
            )
            for trade in kraken_state.trade_history
        )
        if not has_open_order and not has_recent_fill:
            ghost_positions.append(
                GhostPosition(
                    position_id=position.position_id,
                    pair=position.pair,
                    severity=ReconciliationSeverity.HIGH,
                    recommended_action=ReconciliationAction.ALERT,
                )
            )
    return tuple(ghost_positions)


def _detect_foreign_orders(
    kraken_state: KrakenState,
    supabase_state: SupabaseState,
    *,
    as_of: datetime,
    stale_order_age: timedelta,
    cl_ord_id_prefix: str,
) -> tuple[ForeignOrder, ...]:
    foreign_orders: list[ForeignOrder] = []
    acknowledged_exchange_ids = {
        _exchange_order_id(order) for order in supabase_state.orders if _exchange_order_id(order)
    }
    acknowledged_client_ids = {
        _client_order_id(order) for order in supabase_state.orders if _client_order_id(order)
    }
    for order in sorted(kraken_state.open_orders, key=lambda item: item.order_id):
        client_order_id = _client_order_id(order)
        if _is_tracked_client_order_id(client_order_id, cl_ord_id_prefix):
            continue
        age = _age(_opened_at(order), as_of)
        if order.order_id in acknowledged_exchange_ids or client_order_id in acknowledged_client_ids:
            classification = ForeignOrderClassification.ACKED
            severity = ReconciliationSeverity.LOW
            action = ReconciliationAction.AUTO_FIX
            step = "link_foreign_order"
        elif age >= stale_order_age:
            classification = ForeignOrderClassification.STALE
            severity = ReconciliationSeverity.HIGH
            action = ReconciliationAction.ALERT
            step = "manual_review_foreign_order"
        else:
            classification = ForeignOrderClassification.NEW
            severity = ReconciliationSeverity.LOW
            action = ReconciliationAction.AUTO_FIX
            step = "cancel_foreign_order"
        foreign_orders.append(
            ForeignOrder(
                order_id=order.order_id,
                pair=order.pair,
                client_order_id=client_order_id,
                classification=classification,
                age=age,
                severity=severity,
                recommended_action=action,
                recommended_step=step,
            )
        )
    return tuple(foreign_orders)


def _detect_fee_drift(
    kraken_state: KrakenState,
    supabase_state: SupabaseState,
    *,
    fee_drift_tolerance: Decimal,
    high_fee_drift_tolerance: Decimal,
) -> tuple[FeeDrift, ...]:
    fee_drift: list[FeeDrift] = []
    for order in sorted(supabase_state.orders, key=lambda item: item.order_id):
        recorded_fee = order.recorded_fee
        if recorded_fee is None:
            continue
        kraken_fee = sum(
            (trade.fee for trade in kraken_state.trade_history if _trade_matches_order(trade, order)),
            start=ZERO_DECIMAL,
        )
        if kraken_fee == ZERO_DECIMAL:
            continue
        delta = abs(kraken_fee - recorded_fee)
        if delta <= fee_drift_tolerance:
            continue
        if delta > high_fee_drift_tolerance:
            severity = ReconciliationSeverity.HIGH
            action = ReconciliationAction.ALERT
            step = "manual_fee_audit"
        else:
            severity = ReconciliationSeverity.LOW
            action = ReconciliationAction.AUTO_FIX
            step = "sync_fee_ledger"
        fee_drift.append(
            FeeDrift(
                order_id=order.order_id,
                pair=order.pair,
                kraken_fee=kraken_fee,
                supabase_fee=recorded_fee,
                delta=delta,
                severity=severity,
                recommended_action=action,
                recommended_step=step,
            )
        )
    return tuple(fee_drift)


def _detect_untracked_assets(
    kraken_state: KrakenState,
    supabase_state: SupabaseState,
) -> tuple[UntrackedAsset, ...]:
    tracked_assets = {"USD"}
    for position in supabase_state.positions:
        tracked_assets.update(_pair_assets(position.pair))
    for order in supabase_state.orders:
        tracked_assets.update(_pair_assets(order.pair))

    untracked_assets = [
        UntrackedAsset(
            asset=balance.asset,
            available=balance.available,
            held=balance.held,
            severity=ReconciliationSeverity.LOW,
            recommended_action=ReconciliationAction.AUTO_FIX,
        )
        for balance in sorted(kraken_state.balances, key=lambda item: item.asset)
        if balance.available + balance.held > ZERO_DECIMAL and balance.asset not in tracked_assets
    ]
    return tuple(untracked_assets)


def _resolve_as_of(kraken_state: KrakenState, *, as_of: datetime | None) -> datetime:
    if as_of is not None:
        return as_of
    timestamps = [
        *_non_null(_opened_at(order) for order in kraken_state.open_orders),
        *_non_null(_filled_at(trade) for trade in kraken_state.trade_history),
    ]
    return max(timestamps, default=EPOCH)


def _matches_position_order(
    position: SupabasePosition,
    order: KrakenOrder,
    *,
    exchange_ids: set[str],
    client_ids: set[str],
    cl_ord_id_prefix: str,
) -> bool:
    client_order_id = _client_order_id(order)
    return (
        order.order_id in exchange_ids
        or client_order_id in client_ids
        or (
            not exchange_ids
            and not client_ids
            and order.pair == position.pair
            and _is_tracked_client_order_id(client_order_id, cl_ord_id_prefix)
        )
    )


def _is_recent_fill(
    position: SupabasePosition,
    trade: KrakenTrade,
    *,
    exchange_ids: set[str],
    client_ids: set[str],
    window_start: datetime,
) -> bool:
    filled_at = _filled_at(trade)
    if filled_at is None or filled_at < window_start:
        return False
    return (
        _position_id(trade) == position.position_id
        or _order_id(trade) in exchange_ids
        or _client_order_id(trade) in client_ids
        or (not exchange_ids and not client_ids and trade.pair == position.pair)
    )


def _trade_matches_order(trade: KrakenTrade, order: SupabaseOrder) -> bool:
    exchange_order_id = _exchange_order_id(order)
    client_order_id = _client_order_id(order)
    return _order_id(trade) == exchange_order_id or _client_order_id(trade) == client_order_id


def _is_tracked_client_order_id(client_order_id: str | None, prefix: str) -> bool:
    if not client_order_id:
        return False
    return client_order_id.lower().startswith(prefix.lower())


def _pair_assets(pair: str) -> tuple[str, ...]:
    base, separator, quote = pair.partition("/")
    if not separator:
        return (pair,)
    return base, quote


def _age(opened_at: datetime | None, as_of: datetime) -> timedelta:
    if opened_at is None:
        return timedelta(0)
    return as_of - opened_at


def _client_order_id(record: object) -> str | None:
    return getattr(record, "client_order_id", None)


def _exchange_order_id(record: object) -> str | None:
    return getattr(record, "exchange_order_id", None)


def _order_id(record: object) -> str | None:
    return getattr(record, "order_id", None)


def _position_id(record: object) -> str | None:
    return getattr(record, "position_id", None)


def _opened_at(record: object) -> datetime | None:
    return getattr(record, "opened_at", None)


def _filled_at(record: object) -> datetime | None:
    return getattr(record, "filled_at", None)


def _non_null(values: object) -> list[datetime]:
    return [value for value in values if value is not None]


__all__ = [
    "Balance",
    "DEFAULT_CLIENT_ORDER_ID_PREFIX",
    "DEFAULT_FEE_DRIFT_TOLERANCE",
    "DEFAULT_RECENT_FILL_WINDOW",
    "DEFAULT_STALE_ORDER_AGE",
    "FeeDrift",
    "ForeignOrder",
    "ForeignOrderClassification",
    "GhostPosition",
    "KrakenOrder",
    "KrakenState",
    "KrakenTrade",
    "ReconciliationAction",
    "ReconciliationReport",
    "ReconciliationSeverity",
    "SupabaseOrder",
    "SupabasePosition",
    "SupabaseState",
    "UntrackedAsset",
    "reconcile",
]
