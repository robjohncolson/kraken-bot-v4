from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from urllib.request import Request

from exchange.client import KrakenClient, KrakenRateLimiter
from exchange.executor import KrakenExecutor
from exchange.transport import HttpKrakenTransport, RetryPolicy
from exchange.websocket import FillConfirmed, KrakenWebSocketV2


class FakeWebSocket:
    def __init__(self) -> None:
        self._messages: asyncio.Queue[object] = asyncio.Queue()
        self.sent_messages: list[str] = []
        self.close_calls = 0

    def queue_message(self, message: object) -> None:
        self._messages.put_nowait(message)

    async def recv(self) -> str | bytes:
        message = await self._messages.get()
        if isinstance(message, BaseException):
            raise message
        assert isinstance(message, (str, bytes))
        return message

    async def send(self, message: str) -> None:
        self.sent_messages.append(message)

    async def close(self) -> None:
        self.close_calls += 1


class ConnectorScript:
    def __init__(self, *results: object) -> None:
        self._results = list(results)

    async def __call__(self, _: str) -> FakeWebSocket:
        result = self._results.pop(0)
        assert isinstance(result, FakeWebSocket)
        return result


def _response_body(result: object) -> BytesIO:
    body = json.dumps({"error": [], "result": result}).encode()
    response = BytesIO(body)
    response.read = response.read  # noqa: PLW0127
    return response


def _make_executor(sender: object) -> KrakenExecutor:
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
    return KrakenExecutor(
        client=client,
        transport=transport,
        nonce_source=lambda: 1000,
    )


def test_get_ws_token_calls_private_rest_endpoint() -> None:
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        del timeout
        calls.append(req)
        return _response_body({"token": "ws-token-123"})

    executor = _make_executor(sender)

    token = executor.get_ws_token()

    assert token == "ws-token-123"
    assert len(calls) == 1
    assert "GetWebSocketsToken" in calls[0].full_url


def test_subscribe_executions_sends_kraken_v2_message() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        client = KrakenWebSocketV2(connector=ConnectorScript(websocket))

        await client.connect()
        await client.subscribe_executions("ws-token-123")

        assert json.loads(websocket.sent_messages[0]) == {
            "method": "subscribe",
            "params": {
                "channel": "executions",
                "token": "ws-token-123",
            },
        }
        await client.disconnect()

    asyncio.run(scenario())


def test_execution_trade_update_emits_fill_confirmed() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        received: list[FillConfirmed] = []

        async def on_fill(fill: FillConfirmed) -> None:
            received.append(fill)

        client = KrakenWebSocketV2(
            connector=ConnectorScript(websocket),
            fill_handler=on_fill,
        )

        await client.connect()
        websocket.queue_message(
            json.dumps(
                {
                    "channel": "executions",
                    "type": "update",
                    "data": [
                        {
                            "order_id": "OK4GJX-KSTLS-7DZZO5",
                            "cl_ord_id": "kbv4-btcusd-000001",
                            "exec_type": "trade",
                            "symbol": "XBT/USD",
                            "side": "sell",
                            "last_qty": "0.005",
                            "last_price": "26599.9",
                            "timestamp": "2023-09-22T10:33:05.709993Z",
                            "fees": [{"asset": "USD", "qty": "0.3458"}],
                        }
                    ],
                }
            )
        )

        fill = await client.get_fill_confirmed()

        assert fill == FillConfirmed(
            order_id="OK4GJX-KSTLS-7DZZO5",
            client_order_id="kbv4-btcusd-000001",
            pair="BTC/USD",
            side="sell",
            quantity=Decimal("0.005"),
            price=Decimal("26599.9"),
            fee=Decimal("0.3458"),
            timestamp=datetime(2023, 9, 22, 10, 33, 5, 709993, tzinfo=timezone.utc),
        )
        assert received == [fill]
        await client.disconnect()

    asyncio.run(scenario())
