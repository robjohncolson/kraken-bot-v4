"""Pure parsers for Kraken REST API JSON responses.

Each function accepts either a full Kraken response envelope or a
``"result"`` mapping and returns immutable domain types. Both read-side
and mutation response parsing live here.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from core.errors import (
    ExchangeError,
    InsufficientFundsError,
    KrakenAPIError,
    OrderRejectedError,
    RateLimitExceededError,
)
from core.types import Balance, ZERO_DECIMAL
from exchange.models import KrakenOrder, KrakenTrade
from exchange.symbols import normalize_asset_symbol, normalize_pair

__all__ = [
    "KrakenResponseError",
    "parse_add_order_response",
    "parse_balances",
    "parse_cancel_order_response",
    "parse_open_orders",
    "parse_trade_history",
]

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

_STAKING_SUFFIX_RE = re.compile(r"\.[FSMP]$")


class KrakenResponseError(ExchangeError):
    """Raised when a Kraken response has unexpected structure."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"Unexpected Kraken response: {detail}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_staking_suffix(raw_asset: str) -> str:
    """Remove Kraken staking/funding suffixes (``.F``, ``.S``, ``.M``, ``.P``)."""
    return _STAKING_SUFFIX_RE.sub("", raw_asset)


def _safe_timestamp(value: object) -> datetime | None:
    """Convert a numeric timestamp to a UTC *datetime*, or *None*."""
    if value is None:
        return None
    try:
        ts = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if ts == 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _classify_kraken_errors(errors: list[str]) -> KrakenAPIError:
    """Map Kraken API error strings to the project's typed exception hierarchy."""
    for err in errors:
        if err.startswith("EAPI:Rate limit"):
            return RateLimitExceededError(err)
        if err == "EOrder:Insufficient funds":
            return InsufficientFundsError(err)
        if err.startswith("EOrder:"):
            return OrderRejectedError(err)
    return KrakenAPIError("; ".join(errors))


def _unwrap_result(response: Mapping[str, object]) -> Mapping[str, object]:
    """Accept either a full Kraken response envelope or a bare result mapping."""
    if "error" not in response and "result" not in response:
        return response

    raw_errors = response.get("error", [])
    if raw_errors:
        if not isinstance(raw_errors, (list, tuple)) or any(
            not isinstance(err, str) for err in raw_errors
        ):
            raise KrakenResponseError("Invalid 'error' list in Kraken response")
        raise _classify_kraken_errors(list(raw_errors))

    raw_result = response.get("result")
    if not isinstance(raw_result, Mapping):
        raise KrakenResponseError("Missing or invalid 'result' mapping")
    return raw_result


# ---------------------------------------------------------------------------
# Public parsers
# ---------------------------------------------------------------------------


def parse_balances(result: Mapping[str, object]) -> tuple[Balance, ...]:
    """Parse Kraken ``/0/private/Balance`` *result* into domain balances.

    * Strips staking suffixes (``.F``, ``.S``, ``.M``, ``.P``)
    * Normalises asset names via :func:`normalize_asset_symbol`
    * Sums duplicates that map to the same normalised asset
    * Filters zero-balance entries
    * Returns a tuple sorted by asset name
    """
    sums: dict[str, Decimal] = defaultdict(lambda: ZERO_DECIMAL)

    for raw_asset, raw_value in result.items():
        stripped = _strip_staking_suffix(raw_asset)
        asset = normalize_asset_symbol(stripped)
        try:
            amount = Decimal(str(raw_value))
        except (InvalidOperation, TypeError) as exc:
            raise KrakenResponseError(
                f"Cannot parse balance value for {raw_asset!r}: {raw_value!r}"
            ) from exc
        sums[asset] = sums[asset] + amount

    balances = [
        Balance(asset=asset, available=amount, held=ZERO_DECIMAL)
        for asset, amount in sums.items()
        if amount != ZERO_DECIMAL
    ]
    balances.sort(key=lambda b: b.asset)
    return tuple(balances)


def parse_add_order_response(response: Mapping[str, object]) -> tuple[str, ...]:
    """Parse Kraken ``AddOrder`` output and return immutable txids."""
    result = _unwrap_result(response)
    raw_txids = result.get("txid")
    if not isinstance(raw_txids, (list, tuple)) or not raw_txids:
        raise KrakenResponseError("AddOrder result missing non-empty 'txid' list")

    txids = tuple(raw_txids)
    if any(not isinstance(txid, str) or not txid for txid in txids):
        raise KrakenResponseError("AddOrder result contains invalid txid values")
    return txids


def parse_cancel_order_response(response: Mapping[str, object]) -> int:
    """Parse Kraken ``CancelOrder`` output and return the cancel count."""
    result = _unwrap_result(response)
    raw_count = result.get("count")
    if isinstance(raw_count, bool):
        raise KrakenResponseError("CancelOrder result contains invalid 'count' value")

    if isinstance(raw_count, int):
        count = raw_count
    elif isinstance(raw_count, str):
        stripped = raw_count.strip()
        digits = stripped[1:] if stripped.startswith(("+", "-")) else stripped
        if not digits.isdigit():
            raise KrakenResponseError("CancelOrder result missing integer 'count'")
        count = int(stripped)
    else:
        raise KrakenResponseError("CancelOrder result missing integer 'count'")

    if count < 0:
        raise KrakenResponseError("CancelOrder result contains negative 'count'")
    return count


def parse_open_orders(result: Mapping[str, object]) -> tuple[KrakenOrder, ...]:
    """Parse Kraken ``/0/private/OpenOrders`` *result* into domain orders.

    Handles a missing ``"open"`` key gracefully (returns empty tuple).
    """
    open_dict = result.get("open")
    if not open_dict or not isinstance(open_dict, Mapping):
        return ()

    orders: list[KrakenOrder] = []
    for order_id, order_data in open_dict.items():
        if not isinstance(order_data, Mapping):
            raise KrakenResponseError(
                f"Order data for {order_id!r} is not a mapping"
            )

        descr = order_data.get("descr")
        if not isinstance(descr, Mapping):
            raise KrakenResponseError(
                f"Missing or invalid 'descr' for order {order_id!r}"
            )

        pair = normalize_pair(descr["pair"])
        client_order_id = order_data.get("cl_ord_id") or None
        opened_at = _safe_timestamp(order_data.get("opentm"))

        orders.append(
            KrakenOrder(
                order_id=order_id,
                pair=pair,
                client_order_id=client_order_id,
                opened_at=opened_at,
            )
        )

    orders.sort(key=lambda o: o.order_id)
    return tuple(orders)


def parse_trade_history(result: Mapping[str, object]) -> tuple[KrakenTrade, ...]:
    """Parse Kraken ``/0/private/TradesHistory`` *result* into domain trades.

    Handles a missing ``"trades"`` key gracefully (returns empty tuple).
    """
    trades_dict = result.get("trades")
    if not trades_dict or not isinstance(trades_dict, Mapping):
        return ()

    trades: list[KrakenTrade] = []
    for trade_id, trade_data in trades_dict.items():
        if not isinstance(trade_data, Mapping):
            raise KrakenResponseError(
                f"Trade data for {trade_id!r} is not a mapping"
            )

        pair = normalize_pair(trade_data["pair"])
        order_id = trade_data.get("ordertxid") or None
        client_order_id = trade_data.get("cl_ord_id") or None

        try:
            fee = Decimal(str(trade_data["fee"]))
        except (InvalidOperation, TypeError, KeyError) as exc:
            raise KrakenResponseError(
                f"Cannot parse fee for trade {trade_id!r}"
            ) from exc

        filled_at = _safe_timestamp(trade_data.get("time"))

        raw_position_id = trade_data.get("postxid")
        position_id = raw_position_id if raw_position_id else None

        trades.append(
            KrakenTrade(
                trade_id=trade_id,
                pair=pair,
                order_id=order_id,
                client_order_id=client_order_id,
                position_id=position_id,
                fee=fee,
                filled_at=filled_at,
            )
        )

    trades.sort(key=lambda t: t.trade_id)
    return tuple(trades)
