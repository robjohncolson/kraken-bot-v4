"""Read-only Kraken REST executor.

Composes KrakenClient (rate limiting + request preparation),
transport (signing + HTTP), and parsers (JSON → domain types)
into a single high-level API for fetching exchange state.

Mutation methods (execute_order, execute_cancel) are deferred
to Task 2B.
"""

from __future__ import annotations

import logging

from core.types import Balance
from exchange.client import KrakenClient, PreparedKrakenRequest
from exchange.models import KrakenOrder, KrakenState, KrakenTrade
from exchange.parsers import parse_balances, parse_open_orders, parse_trade_history
from exchange.transport import (
    HttpKrakenTransport,
    NonceSource,
    make_default_nonce_source,
    sign_request,
)

logger = logging.getLogger(__name__)


class KrakenExecutor:
    """Fetch exchange state from live Kraken via authenticated REST calls.

    Read-only: no order placement or cancellation.
    """

    def __init__(
        self,
        *,
        client: KrakenClient,
        transport: HttpKrakenTransport,
        nonce_source: NonceSource | None = None,
    ) -> None:
        self._client = client
        self._transport = transport
        self._nonce_source = nonce_source or make_default_nonce_source()

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

    def _execute(self, prepared: PreparedKrakenRequest) -> dict[str, object]:
        signed = sign_request(
            self._client.api_key,
            self._client.api_secret,
            prepared.endpoint,
            dict(prepared.payload),
            nonce_source=self._nonce_source,
        )
        return self._transport.send(signed)


__all__ = [
    "KrakenExecutor",
]
