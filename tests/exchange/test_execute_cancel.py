from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

from core.errors import SafeModeBlockedError
from core.types import CircuitBreakerState
from exchange.client import KrakenClient, KrakenRateLimiter
from exchange.executor import KrakenExecutor
from exchange.order_gate import OrderGate, OrderMutationBlockedError
from exchange.transport import HttpKrakenTransport, RetryPolicy

NOW = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
ORDER_ID = "O3KQ7H-AAAA-BBBBBB"


class ManualClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def now(self) -> float:
        return self.current


def _response_body(result: object) -> BytesIO:
    body = json.dumps({"error": [], "result": result}).encode()
    response = BytesIO(body)
    response.read = response.read  # noqa: PLW0127
    return response


def _open_orders_result(*, opened_at: datetime) -> dict[str, object]:
    return {
        "open": {
            ORDER_ID: {
                "descr": {"pair": "XDGUSD"},
                "opentm": opened_at.timestamp(),
                "cl_ord_id": "kbv4-dogeusd-000001",
            }
        }
    }


def _make_executor(
    sender: object,
    *,
    order_gate: OrderGate | None = None,
    utc_now: datetime = NOW,
    read_only_exchange: bool = False,
    disable_order_mutations: bool = False,
) -> KrakenExecutor:
    secret = base64.b64encode(b"test_secret").decode()
    client = KrakenClient(
        api_key="test_key",
        api_secret=secret,
        rate_limiter=KrakenRateLimiter(now=lambda: 0.0),
    )
    transport = HttpKrakenTransport(
        retry_policy=RetryPolicy(max_retries=0),
        sender=sender,  # type: ignore[arg-type]
    )
    gate = order_gate or OrderGate(client=client)
    return KrakenExecutor(
        client=client,
        transport=transport,
        nonce_source=lambda: 1000,
        order_gate=gate,
        utc_now=lambda: utc_now,
        read_only_exchange=read_only_exchange,
        disable_order_mutations=disable_order_mutations,
    )


def test_execute_cancel_posts_cancel_order_after_resolving_open_order() -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        if "OpenOrders" in req.full_url:
            return _response_body(_open_orders_result(opened_at=NOW - timedelta(seconds=10)))
        return _response_body({"count": 1})

    executor = _make_executor(sender)

    cancel_count = executor.execute_cancel(ORDER_ID)

    assert cancel_count == 1
    assert len(calls) == 2
    assert "OpenOrders" in calls[0].full_url
    assert "CancelOrder" in calls[1].full_url
    assert calls[1].get_header("Api-key") == "test_key"
    assert calls[1].get_header("Api-sign") is not None

    request_fields = parse_qs(calls[1].data.decode())
    assert request_fields["pair"] == ["DOGE/USD"]
    assert request_fields["txid"] == [ORDER_ID]


@pytest.mark.parametrize(
    ("read_only_exchange", "disable_order_mutations"),
    ((True, False), (False, True)),
)
def test_execute_cancel_blocks_when_safe_mode_is_enabled(
    *,
    read_only_exchange: bool,
    disable_order_mutations: bool,
) -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        return _response_body({"count": 1})

    executor = _make_executor(
        sender,
        read_only_exchange=read_only_exchange,
        disable_order_mutations=disable_order_mutations,
    )

    with pytest.raises(SafeModeBlockedError, match="safe mode"):
        executor.execute_cancel(ORDER_ID)

    assert calls == []


def test_execute_cancel_trips_circuit_breaker_after_three_failures() -> None:
    calls: list[Request] = []
    clock = ManualClock()
    secret = base64.b64encode(b"test_secret").decode()
    client = KrakenClient(
        api_key="test_key",
        api_secret=secret,
        rate_limiter=KrakenRateLimiter(now=lambda: 0.0),
    )
    order_gate = OrderGate(client=client, now=clock.now)

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        if "OpenOrders" in req.full_url:
            return _response_body(_open_orders_result(opened_at=NOW - timedelta(seconds=10)))
        raise TimeoutError("simulated cancel timeout")

    transport = HttpKrakenTransport(
        retry_policy=RetryPolicy(max_retries=0),
        sender=sender,  # type: ignore[arg-type]
    )
    executor = KrakenExecutor(
        client=client,
        transport=transport,
        nonce_source=lambda: 1000,
        order_gate=order_gate,
        utc_now=lambda: NOW,
        read_only_exchange=False,
        disable_order_mutations=False,
    )

    for _ in range(3):
        with pytest.raises(TimeoutError, match="simulated cancel timeout"):
            executor.execute_cancel(ORDER_ID)

    assert order_gate.circuit_breaker.state == CircuitBreakerState.OPEN
    assert order_gate.circuit_breaker.failure_count == 3

    with pytest.raises(OrderMutationBlockedError):
        executor.execute_cancel(ORDER_ID)

    assert len(calls) == 6


def test_execute_cancel_logs_starter_penalty_warning_for_new_orders(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        if "OpenOrders" in req.full_url:
            return _response_body(_open_orders_result(opened_at=NOW - timedelta(seconds=2)))
        return _response_body({"count": 1})

    executor = _make_executor(sender)

    with caplog.at_level(logging.WARNING):
        cancel_count = executor.execute_cancel(ORDER_ID)

    assert cancel_count == 1
    assert len(calls) == 2
    assert "Starter-tier" in caplog.text
    assert "8-point" in caplog.text
