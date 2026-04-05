from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal

from exchange.models import KrakenTrade
from exchange.websocket import (
    ConnectionState,
    FillConfirmed,
    KrakenWebSocketV2,
    PolledOrderSnapshot,
    PriceTick,
)


class ConnectionDropError(OSError):
    """Typed disconnect used by the fallback polling tests."""


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
        self.call_count = 0

    async def __call__(self, _: str) -> FakeWebSocket:
        self.call_count += 1
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, FakeWebSocket)
        return result


class FailingReconnectConnector:
    def __init__(self, websocket: FakeWebSocket) -> None:
        self._websocket = websocket
        self.call_count = 0

    async def __call__(self, _: str) -> FakeWebSocket:
        self.call_count += 1
        if self.call_count == 1:
            return self._websocket
        raise ConnectionDropError(f"retry-{self.call_count}")


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


async def _wait_for(predicate, *, turns: int = 200) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met")


def test_fallback_poller_activates_on_disconnect_and_deactivates_on_reconnect() -> None:
    async def scenario() -> None:
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        connector = ConnectorScript(
            first_socket,
            ConnectionDropError("retry-1"),
            second_socket,
        )
        sleep = SleepRecorder()
        price_calls: list[tuple[str, ...]] = []
        order_polls = 0

        async def poll_ticker(pairs: tuple[str, ...]) -> tuple[PriceTick, ...]:
            price_calls.append(pairs)
            return ()

        async def poll_open_orders() -> tuple[PolledOrderSnapshot, ...]:
            nonlocal order_polls
            order_polls += 1
            return ()

        client = KrakenWebSocketV2(
            connector=connector,
            sleep=sleep,
            rest_ticker_poller=poll_ticker,
            rest_open_orders_poller=poll_open_orders,
            price_poll_interval_sec=1.5,
            order_poll_interval_sec=3.5,
        )

        await client.connect()
        await client.subscribe_ticker(["XBT/USD"])
        await client.subscribe_executions("ws-token-123")

        first_socket.queue_message(ConnectionDropError("dropped"))

        await _wait_for(
            lambda: (
                client.state is ConnectionState.RECONNECTING
                and client.fallback_poller.active
            )
        )
        await _wait_for(lambda: 1.5 in sleep.delays and 3.5 in sleep.delays)
        await _wait_for(
            lambda: (
                client.state is ConnectionState.CONNECTED
                and connector.call_count == 3
                and not client.fallback_poller.active
            )
        )

        assert price_calls
        assert all(pairs == ("BTC/USD",) for pairs in price_calls)
        assert order_polls >= 1
        assert any(
            json.loads(message)
            == {
                "method": "subscribe",
                "params": {"channel": "ticker", "symbol": ["BTC/USD"]},
            }
            for message in second_socket.sent_messages
        )
        assert any(
            json.loads(message)
            == {
                "method": "subscribe",
                "params": {"channel": "executions", "token": "ws-token-123"},
            }
            for message in second_socket.sent_messages
        )

        await client.disconnect()

    asyncio.run(scenario())


def test_fallback_poller_emits_polled_price_ticks_and_fill_confirmations() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connector = FailingReconnectConnector(websocket)
        sleep = SleepRecorder()
        now = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
        polled_tick = PriceTick(
            pair="BTC/USD",
            bid=Decimal("86000.1"),
            ask=Decimal("86000.2"),
            last=Decimal("86000.15"),
            timestamp=now,
        )
        tick_callbacks: list[PriceTick] = []
        fill_callbacks: list[FillConfirmed] = []
        ticker_polls = 0
        open_order_polls = 0
        trade_history_polls = 0

        async def on_tick(tick: PriceTick) -> None:
            tick_callbacks.append(tick)

        async def on_fill(fill: FillConfirmed) -> None:
            fill_callbacks.append(fill)

        async def poll_ticker(_: tuple[str, ...]) -> tuple[PriceTick, ...]:
            nonlocal ticker_polls
            ticker_polls += 1
            if ticker_polls == 1:
                return (polled_tick,)
            return ()

        async def poll_open_orders() -> tuple[PolledOrderSnapshot, ...]:
            nonlocal open_order_polls
            open_order_polls += 1
            if open_order_polls == 1:
                return (
                    PolledOrderSnapshot(
                        order_id="O-123",
                        client_order_id="cid-123",
                        pair="BTC/USD",
                        side="buy",
                        quantity=Decimal("0.010"),
                        price=Decimal("86000.15"),
                        fee=Decimal("0.21"),
                    ),
                )
            return ()

        async def poll_trade_history() -> tuple[KrakenTrade, ...]:
            nonlocal trade_history_polls
            trade_history_polls += 1
            return (
                KrakenTrade(
                    trade_id="T-123",
                    pair="BTC/USD",
                    order_id="O-123",
                    client_order_id="cid-123",
                    side="buy",
                    quantity=Decimal("0.012"),
                    price=Decimal("85990.5"),
                    fee=Decimal("0.18"),
                    filled_at=now,
                ),
            )

        client = KrakenWebSocketV2(
            connector=connector,
            sleep=sleep,
            ticker_handler=on_tick,
            fill_handler=on_fill,
            rest_ticker_poller=poll_ticker,
            rest_open_orders_poller=poll_open_orders,
            rest_trade_history_poller=poll_trade_history,
            price_poll_interval_sec=1.0,
            order_poll_interval_sec=1.0,
            utc_now=lambda: now,
        )

        await client.connect()
        await client.subscribe_ticker(["XBT/USD"])

        websocket.queue_message(ConnectionDropError("dropped"))

        await _wait_for(
            lambda: (
                client.state
                in (ConnectionState.DISCONNECTED, ConnectionState.RECONNECTING)
                and client.fallback_poller.active
            )
        )

        tick = await client.get_price_tick()
        fill = await client.get_fill_confirmed()

        assert tick == polled_tick
        assert fill == FillConfirmed(
            order_id="O-123",
            client_order_id="cid-123",
            pair="BTC/USD",
            side="buy",
            quantity=Decimal("0.012"),
            price=Decimal("85990.5"),
            fee=Decimal("0.18"),
            timestamp=now,
        )
        assert tick_callbacks == [tick]
        assert fill_callbacks == [fill]
        assert trade_history_polls >= 1

        await client.disconnect()

    asyncio.run(scenario())
