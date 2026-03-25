from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

from exchange.websocket import KrakenWebSocketV2, PriceTick


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


def test_subscribe_ticker_sends_kraken_v2_message() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        client = KrakenWebSocketV2(connector=ConnectorScript(websocket))

        await client.connect()
        await client.subscribe_ticker(["XBT/USD", "DOGE-USD"])

        assert json.loads(websocket.sent_messages[0]) == {
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "symbol": ["BTC/USD", "DOGE/USD"],
            },
        }
        await client.disconnect()

    asyncio.run(scenario())


def test_ticker_update_emits_price_tick_to_queue_with_normalized_symbol() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        client = KrakenWebSocketV2(connector=ConnectorScript(websocket))

        await client.connect()
        websocket.queue_message(
            json.dumps(
                {
                    "channel": "ticker",
                    "type": "update",
                    "data": [
                        {
                            "symbol": "XBT/USD",
                            "bid": "86000.1",
                            "ask": "86000.2",
                            "last": "86000.15",
                            "timestamp": "2026-03-25T12:00:00Z",
                        }
                    ],
                }
            )
        )

        tick = await client.get_price_tick()

        assert tick == PriceTick(
            pair="BTC/USD",
            bid=Decimal("86000.1"),
            ask=Decimal("86000.2"),
            last=Decimal("86000.15"),
            timestamp=datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc),
        )
        await client.disconnect()

    asyncio.run(scenario())


def test_ticker_update_invokes_callback_for_nested_price_fields() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        received: list[PriceTick] = []

        async def on_tick(tick: PriceTick) -> None:
            received.append(tick)

        client = KrakenWebSocketV2(
            connector=ConnectorScript(websocket),
            ticker_handler=on_tick,
        )

        await client.connect()
        websocket.queue_message(
            json.dumps(
                {
                    "channel": "ticker",
                    "type": "snapshot",
                    "time_in": 1774449600,
                    "data": [
                        {
                            "symbol": "BTC/USD",
                            "bid": {"price": "87000.1"},
                            "ask": {"price": "87000.2"},
                            "last": {"price": "87000.15"},
                        }
                    ],
                }
            )
        )

        tick = await client.get_price_tick()

        assert received == [tick]
        assert tick.timestamp == datetime.fromtimestamp(1774449600, tz=timezone.utc)
        await client.disconnect()

    asyncio.run(scenario())
