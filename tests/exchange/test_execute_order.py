from __future__ import annotations

import base64
import json
from decimal import Decimal
from io import BytesIO
from urllib.parse import parse_qs
from urllib.request import Request

import pytest

from core.errors import SafeModeBlockedError
from core.types import OrderRequest, OrderSide, OrderType
from exchange.client import KrakenClient, KrakenRateLimiter
from exchange.executor import KrakenExecutor
from exchange.order_gate import OrderGate
from exchange.transport import HttpKrakenTransport, RetryPolicy


def _response_body(result: object) -> BytesIO:
    body = json.dumps({"error": [], "result": result}).encode()
    response = BytesIO(body)
    response.read = response.read  # noqa: PLW0127
    return response


def _make_executor(
    sender: object,
    *,
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
    order_gate = OrderGate(
        client=client,
        sequence_source=iter((1,)).__next__,
        kraken_tier="intermediate",
    )
    return KrakenExecutor(
        client=client,
        transport=transport,
        nonce_source=lambda: 1000,
        order_gate=order_gate,
        read_only_exchange=read_only_exchange,
        disable_order_mutations=disable_order_mutations,
    )


def _order_request() -> OrderRequest:
    return OrderRequest(
        pair="dogeusd",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("25"),
        limit_price=Decimal("0.1250"),
    )


def test_execute_order_places_and_verifies_order() -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        if "AddOrder" in req.full_url:
            return _response_body({"txid": ["O3KQ7H-AAAA-BBBBBB"]})
        return _response_body(
            {
                "open": {
                    "O3KQ7H-AAAA-BBBBBB": {
                        "descr": {"pair": "XDGUSD"},
                        "opentm": 1711296000.0,
                        "cl_ord_id": "kbv4-dogeusd-000001",
                    }
                }
            }
        )

    executor = _make_executor(sender)

    order_id = executor.execute_order(_order_request())

    assert order_id == "O3KQ7H-AAAA-BBBBBB"
    assert len(calls) == 2
    assert "AddOrder" in calls[0].full_url
    assert "OpenOrders" in calls[1].full_url
    assert calls[0].get_header("Api-key") == "test_key"
    assert calls[0].get_header("Api-sign") is not None

    request_fields = parse_qs(calls[0].data.decode())
    assert request_fields["cl_ord_id"] == ["kbv4-dogeusd-000001"]
    assert request_fields["pair"] == ["DOGE/USD"]
    assert request_fields["ordertype"] == ["limit"]
    assert request_fields["type"] == ["buy"]


def test_execute_order_blocks_when_safe_mode_is_enabled() -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        return _response_body({"txid": ["unexpected"]})

    executor = _make_executor(
        sender,
        read_only_exchange=True,
        disable_order_mutations=False,
    )

    with pytest.raises(SafeModeBlockedError, match="safe mode"):
        executor.execute_order(_order_request())

    assert calls == []


def test_execute_order_recovers_ghost_order_by_client_order_id_after_timeout() -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        if "AddOrder" in req.full_url:
            raise TimeoutError("simulated timeout")
        return _response_body(
            {
                "open": {
                    "O3KQ7H-AAAA-BBBBBB": {
                        "descr": {"pair": "XDGUSD"},
                        "opentm": 1711296000.0,
                        "cl_ord_id": "kbv4-dogeusd-000001",
                    }
                }
            }
        )

    executor = _make_executor(sender)

    order_id = executor.execute_order(_order_request())

    assert order_id == "O3KQ7H-AAAA-BBBBBB"
    assert len(calls) == 2
    assert "AddOrder" in calls[0].full_url
    assert "OpenOrders" in calls[1].full_url
