from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping

from core.errors import KrakenBotError
from exchange.pair_metadata import PairMetadataCache
from core.types import Pair, Price, Quantity, UsdAmount, ZERO_DECIMAL


class GridSizingError(KrakenBotError):
    """Base exception for grid slot sizing failures."""


class UnknownPairMinimumError(GridSizingError):
    """Raised when a pair has no configured Kraken minimum quantity."""

    def __init__(self, pair: Pair) -> None:
        self.pair = pair
        super().__init__(f"No Kraken minimum trade size is configured for pair {pair!r}.")


class InvalidReferencePriceError(GridSizingError):
    """Raised when slot sizing is requested with a non-positive reference price."""

    def __init__(self, pair: Pair, price: Price) -> None:
        self.pair = pair
        self.price = price
        super().__init__(f"Reference price for pair {pair!r} must be positive; got {price}.")


class InvalidAvailableCapitalError(GridSizingError):
    """Raised when available slot capital is negative."""

    def __init__(self, capital_usd: UsdAmount) -> None:
        self.capital_usd = capital_usd
        super().__init__(f"Available capital must be non-negative; got {capital_usd}.")


_pair_metadata_cache: PairMetadataCache | None = None


def set_pair_metadata_cache(cache: PairMetadataCache) -> None:
    """Set the shared pair metadata cache for dynamic ordermin lookups."""
    global _pair_metadata_cache
    _pair_metadata_cache = cache


KRAKEN_MINIMUM_ORDER_QUANTITIES: Mapping[Pair, Quantity] = MappingProxyType(
    {
        "ADA/USD": Decimal("10"),
        "BTC/USD": Decimal("0.00005"),
        "DOGE/USD": Decimal("50"),
        "ETH/USD": Decimal("0.002"),
        "SOL/USD": Decimal("0.1"),
        "XBT/USD": Decimal("0.00005"),
        "XRP/USD": Decimal("10"),
    }
)


@dataclass(frozen=True, slots=True)
class SlotAllocation:
    pair: Pair
    available_capital_usd: UsdAmount
    minimum_quantity: Quantity
    minimum_slot_size_usd: UsdAmount
    slot_count: int
    allocated_capital_usd: UsdAmount
    remainder_usd: UsdAmount


def min_slot_size_usd(pair: Pair, reference_price: Price) -> UsdAmount:
    """Return the USD notional required for Kraken's minimum order size on a pair."""

    minimum_quantity = _minimum_order_quantity(pair)
    validated_price = _validate_reference_price(pair, reference_price)
    return minimum_quantity * validated_price


def calculate_slot_count(
    available_capital_usd: UsdAmount,
    pair: Pair,
    reference_price: Price,
) -> SlotAllocation:
    """Split capital into the maximum number of minimum-sized grid slots."""

    validated_capital = _validate_available_capital(available_capital_usd)
    minimum_quantity = _minimum_order_quantity(pair)
    minimum_slot_size = min_slot_size_usd(pair, reference_price)

    slot_count = int(validated_capital // minimum_slot_size)
    allocated_capital = minimum_slot_size * Decimal(slot_count)
    remainder = validated_capital - allocated_capital

    return SlotAllocation(
        pair=pair,
        available_capital_usd=validated_capital,
        minimum_quantity=minimum_quantity,
        minimum_slot_size_usd=minimum_slot_size,
        slot_count=slot_count,
        allocated_capital_usd=allocated_capital,
        remainder_usd=remainder,
    )


def _minimum_order_quantity(pair: Pair) -> Quantity:
    # Dynamic cache takes priority over hardcoded map
    if _pair_metadata_cache is not None:
        ordermin = _pair_metadata_cache.ordermin(pair)
        if ordermin is not None:
            return ordermin
    try:
        return KRAKEN_MINIMUM_ORDER_QUANTITIES[pair]
    except KeyError as exc:
        raise UnknownPairMinimumError(pair) from exc


def _validate_reference_price(pair: Pair, reference_price: Price) -> Price:
    if reference_price <= ZERO_DECIMAL:
        raise InvalidReferencePriceError(pair, reference_price)
    return reference_price


def _validate_available_capital(available_capital_usd: UsdAmount) -> UsdAmount:
    if available_capital_usd < ZERO_DECIMAL:
        raise InvalidAvailableCapitalError(available_capital_usd)
    return available_capital_usd


__all__ = [
    "calculate_slot_count",
    "GridSizingError",
    "InvalidAvailableCapitalError",
    "InvalidReferencePriceError",
    "KRAKEN_MINIMUM_ORDER_QUANTITIES",
    "min_slot_size_usd",
    "SlotAllocation",
    "set_pair_metadata_cache",
    "UnknownPairMinimumError",
]
