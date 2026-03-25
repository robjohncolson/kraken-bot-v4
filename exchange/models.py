"""Domain types for Kraken exchange data.

These types represent data fetched from the Kraken REST API.
They live in exchange/ (not trading/) to keep the dependency
direction clean: trading depends on exchange, not vice versa.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from core.types import (
    Balance,
    ClientOrderId,
    OrderId,
    Pair,
    PositionId,
    ZERO_DECIMAL,
)


@dataclass(frozen=True, slots=True)
class KrakenOrder:
    order_id: OrderId
    pair: Pair
    client_order_id: ClientOrderId | None = None
    opened_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KrakenTrade:
    trade_id: str
    pair: Pair
    order_id: OrderId | None = None
    client_order_id: ClientOrderId | None = None
    position_id: PositionId | None = None
    fee: Decimal = ZERO_DECIMAL
    filled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class KrakenState:
    balances: tuple[Balance, ...] = field(default_factory=tuple)
    open_orders: tuple[KrakenOrder, ...] = field(default_factory=tuple)
    trade_history: tuple[KrakenTrade, ...] = field(default_factory=tuple)


__all__ = [
    "KrakenOrder",
    "KrakenState",
    "KrakenTrade",
]
