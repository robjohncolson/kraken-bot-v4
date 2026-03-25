"""Kraken REST executor.

Composes KrakenClient (rate limiting + request preparation),
transport (signing + HTTP), and parsers (JSON to domain types)
into a single high-level API for fetching exchange state and
executing the current mutation surface.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

from core.errors import ExchangeError, SafeModeBlockedError
from core.types import Balance, OrderRequest
from exchange.client import (
    STARTER_CANCEL_PENALTY_POINTS,
    STARTER_CANCEL_PENALTY_THRESHOLD_SECONDS,
    KrakenClient,
    PreparedKrakenRequest,
)
from exchange.models import KrakenOrder, KrakenState, KrakenTrade
from exchange.order_gate import OrderGate, OrderMutationCircuitBreaker
from exchange.parsers import (
    KrakenResponseError,
    parse_add_order_response,
    parse_balances,
    parse_cancel_order_response,
    parse_open_orders,
    parse_trade_history,
)
from exchange.transport import (
    HttpKrakenTransport,
    KrakenTransportError,
    NonceSource,
    make_default_nonce_source,
    sign_request,
)

logger = logging.getLogger(__name__)


class AmbiguousOrderResultError(ExchangeError):
    """Raised when AddOrder may have succeeded but could not be confirmed."""

    def __init__(self, client_order_id: str) -> None:
        self.client_order_id = client_order_id
        super().__init__(
            "Unable to confirm AddOrder outcome for "
            f"client_order_id={client_order_id!r}."
        )


class OrderVerificationError(ExchangeError):
    """Raised when AddOrder succeeds but the order cannot be verified afterward."""

    def __init__(self, txid: str, client_order_id: str) -> None:
        self.txid = txid
        self.client_order_id = client_order_id
        super().__init__(
            "AddOrder returned a txid but GetOpenOrders could not verify "
            f"txid={txid!r}, client_order_id={client_order_id!r}."
        )


class CancelOrderNotFoundError(ExchangeError):
    """Raised when a requested cancel target is not present in open orders."""

    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        super().__init__(f"Open order {order_id!r} was not found for cancellation.")


class CancelRejectedError(ExchangeError):
    """Raised when Kraken reports that a cancel request affected zero orders."""

    def __init__(self, order_id: str, cancel_count: int) -> None:
        self.order_id = order_id
        self.cancel_count = cancel_count
        super().__init__(f"CancelOrder did not cancel {order_id!r}; count={cancel_count}.")


class KrakenExecutor:
    """Fetch exchange state and execute authenticated Kraken REST mutations."""

    def __init__(
        self,
        *,
        client: KrakenClient,
        transport: HttpKrakenTransport,
        nonce_source: NonceSource | None = None,
        order_gate: OrderGate | None = None,
        utc_now: Callable[[], datetime] | None = None,
        read_only_exchange: bool = True,
        disable_order_mutations: bool = True,
    ) -> None:
        self._client = client
        self._transport = transport
        self._nonce_source = nonce_source or make_default_nonce_source()
        self._order_gate = order_gate or OrderGate(client=client)
        self._utc_now = utc_now or _default_utc_now
        self._read_only_exchange = read_only_exchange
        self._disable_order_mutations = disable_order_mutations

    def fetch_balances(self) -> tuple[Balance, ...]:
        prepared = self._client.get_balances()
        result = self._execute(prepared)
        return parse_balances(result)

    def fetch_open_orders(self) -> tuple[KrakenOrder, ...]:
        prepared = self._client.get_open_orders()
        result = self._execute(prepared)
        return parse_open_orders(result)

    def fetch_trade_history(self) -> tuple[KrakenTrade, ...]:
        prepared = self._client.get_trade_history()
        result = self._execute(prepared)
        return parse_trade_history(result)

    def execute_order(self, order: OrderRequest) -> str:
        self._ensure_mutations_enabled()
        prepared = self._order_gate.place_order(order)
        client_order_id = _require_client_order_id(prepared)

        try:
            result = self._execute(prepared)
            txid = parse_add_order_response(result)[0]
        except (
            HTTPError,
            KrakenResponseError,
            KrakenTransportError,
            TimeoutError,
            URLError,
        ) as exc:
            recovered_order = self._recover_open_order(
                client_order_id=client_order_id,
                failure=exc,
            )
            logger.warning(
                "Recovered ambiguous AddOrder outcome via cl_ord_id=%s -> %s",
                client_order_id,
                recovered_order.order_id,
            )
            return recovered_order.order_id

        verified_order = self._verify_open_order(
            txid=txid,
            client_order_id=client_order_id,
        )
        return verified_order.order_id

    def execute_cancel(self, order_id: str) -> int:
        self._ensure_mutations_enabled()
        breaker = self._mutation_breaker()
        breaker.before_mutation()

        try:
            open_order = self._get_open_order_for_cancel(order_id)
            order_age_seconds = _order_age_seconds(open_order, now=self._utc_now())
            self._warn_if_cancel_penalty(
                order_id=open_order.order_id,
                order_age_seconds=order_age_seconds,
            )
            prepared = self._client.cancel_order(
                open_order.pair,
                open_order.order_id,
                order_age_seconds=order_age_seconds,
            )
            result = self._execute(prepared)
            cancel_count = parse_cancel_order_response(result)
        except CancelOrderNotFoundError:
            raise
        except (
            ExchangeError,
            HTTPError,
            KrakenResponseError,
            KrakenTransportError,
            TimeoutError,
            URLError,
        ):
            breaker.record_failure()
            raise

        if cancel_count < 1:
            breaker.record_failure()
            raise CancelRejectedError(open_order.order_id, cancel_count)

        breaker.record_success()
        logger.info("Canceled Kraken order %s", open_order.order_id)
        return cancel_count

    def fetch_kraken_state(self) -> KrakenState:
        balances = self.fetch_balances()
        open_orders = self.fetch_open_orders()
        trade_history = self.fetch_trade_history()
        logger.info(
            "Fetched Kraken state: %d balances, %d open orders, %d trades",
            len(balances),
            len(open_orders),
            len(trade_history),
        )
        return KrakenState(
            balances=balances,
            open_orders=open_orders,
            trade_history=trade_history,
        )

    def _ensure_mutations_enabled(self) -> None:
        if self._disable_order_mutations or self._read_only_exchange:
            raise SafeModeBlockedError("Order mutations are disabled by safe mode.")

    def _recover_open_order(
        self,
        *,
        client_order_id: str,
        failure: BaseException,
    ) -> KrakenOrder:
        try:
            open_orders = self.fetch_open_orders()
        except (ExchangeError, HTTPError, TimeoutError, URLError) as recovery_exc:
            raise AmbiguousOrderResultError(client_order_id) from recovery_exc

        recovered_order = _find_open_order(
            open_orders,
            client_order_id=client_order_id,
        )
        if recovered_order is None:
            raise AmbiguousOrderResultError(client_order_id) from failure
        return recovered_order

    def _verify_open_order(
        self,
        *,
        txid: str,
        client_order_id: str,
    ) -> KrakenOrder:
        open_orders = self.fetch_open_orders()
        verified_order = _find_open_order(
            open_orders,
            txid=txid,
            client_order_id=client_order_id,
        )
        if verified_order is None:
            raise OrderVerificationError(txid, client_order_id)
        return verified_order

    def _get_open_order_for_cancel(self, order_id: str) -> KrakenOrder:
        normalized_order_id = order_id.strip()
        if not normalized_order_id:
            raise CancelOrderNotFoundError(order_id)

        open_order = _find_open_order(
            self.fetch_open_orders(),
            txid=normalized_order_id,
        )
        if open_order is None:
            raise CancelOrderNotFoundError(normalized_order_id)
        return open_order

    def _mutation_breaker(self) -> OrderMutationCircuitBreaker:
        return self._order_gate._breaker

    def _warn_if_cancel_penalty(
        self,
        *,
        order_id: str,
        order_age_seconds: float,
    ) -> None:
        if order_age_seconds >= float(STARTER_CANCEL_PENALTY_THRESHOLD_SECONDS):
            return
        logger.warning(
            "Canceling order %s after %.3fs triggers the Kraken Starter-tier %d-point penalty.",
            order_id,
            order_age_seconds,
            STARTER_CANCEL_PENALTY_POINTS,
        )

    def _execute(self, prepared: PreparedKrakenRequest) -> dict[str, object]:
        signed = sign_request(
            self._client.api_key,
            self._client.api_secret,
            prepared.endpoint,
            dict(prepared.payload),
            nonce_source=self._nonce_source,
        )
        return self._transport.send(signed)


def _require_client_order_id(prepared: PreparedKrakenRequest) -> str:
    client_order_id = prepared.payload.get("cl_ord_id")
    if isinstance(client_order_id, str) and client_order_id:
        return client_order_id
    raise ExchangeError("Prepared AddOrder request missing cl_ord_id.")


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _order_age_seconds(order: KrakenOrder, *, now: datetime) -> float:
    if order.opened_at is None:
        return float(STARTER_CANCEL_PENALTY_THRESHOLD_SECONDS)

    opened_at = order.opened_at
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - opened_at).total_seconds())


def _find_open_order(
    open_orders: tuple[KrakenOrder, ...],
    *,
    txid: str | None = None,
    client_order_id: str | None = None,
) -> KrakenOrder | None:
    for order in open_orders:
        if txid is not None and order.order_id == txid:
            return order
        if client_order_id is not None and order.client_order_id == client_order_id:
            return order
    return None


__all__ = [
    "CancelOrderNotFoundError",
    "CancelRejectedError",
    "AmbiguousOrderResultError",
    "KrakenExecutor",
    "OrderVerificationError",
]
