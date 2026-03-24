from __future__ import annotations

from decimal import Decimal

import pytest

from core.errors import OrderRejectedError
from core.types import CircuitBreakerState, OrderRequest, OrderSide, OrderType
from exchange.client import KrakenClient, PreparedKrakenRequest
from exchange.order_gate import OrderGate, OrderMutationBlockedError


class ManualClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def now(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FailingClient:
    def __init__(self) -> None:
        self.place_calls = 0
        self.cancel_calls = 0

    def place_order(
        self,
        pair: str,
        payload: dict[str, object],
    ) -> PreparedKrakenRequest:
        del pair, payload
        self.place_calls += 1
        raise OrderRejectedError("typed failure")

    def cancel_order(
        self,
        pair: str,
        txid: str,
        *,
        order_age_seconds: float,
    ) -> PreparedKrakenRequest:
        del pair, txid, order_age_seconds
        self.cancel_calls += 1
        raise AssertionError("cancel_order should be blocked by the circuit breaker")


def test_place_order_generates_cl_ord_id_for_emitted_payloads() -> None:
    gate = OrderGate(
        client=KrakenClient(api_key="key", api_secret="secret"),
        sequence_source=iter((1,)).__next__,
    )
    order = OrderRequest(
        pair="xxrpzusd",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("100"),
        limit_price=Decimal("0.2500"),
    )

    request = gate.place_order(order)

    assert request.endpoint == "/0/private/AddOrder"
    assert request.payload["pair"] == "XRP/USD"
    assert request.payload["cl_ord_id"] == "kbv4-xrpusd-000001"
    assert request.payload["ordertype"] == "limit"
    assert request.payload["type"] == "buy"
    assert request.payload["volume"] == "100"
    assert request.payload["price"] == "0.2500"


def test_circuit_breaker_blocks_mutations_after_repeated_failures() -> None:
    clock = ManualClock()
    client = FailingClient()
    gate = OrderGate(client=client, now=clock.now)
    order = OrderRequest(
        pair="dogeusd",
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        quantity=Decimal("25"),
        limit_price=Decimal("0.125"),
    )

    for _ in range(3):
        with pytest.raises(OrderRejectedError):
            gate.place_order(order)

    snapshot = gate.circuit_breaker

    assert client.place_calls == 3
    assert snapshot.state == CircuitBreakerState.OPEN
    assert snapshot.failure_count == 3

    with pytest.raises(OrderMutationBlockedError):
        gate.cancel_order("dogeusd", "tx-1", order_age_seconds=10.0)

    assert client.cancel_calls == 0
