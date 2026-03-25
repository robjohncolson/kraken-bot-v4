from __future__ import annotations

import asyncio
import json

from exchange.websocket import (
    KRAKEN_WEBSOCKET_V2_URL,
    ConnectionState,
    KrakenWebSocketV2,
)


class ConnectionDropError(OSError):
    """Typed disconnect used by the test doubles."""


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
        self.urls: list[str] = []

    @property
    def call_count(self) -> int:
        return len(self.urls)

    async def __call__(self, url: str) -> FakeWebSocket:
        self.urls.append(url)
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, FakeWebSocket)
        return result


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


async def _wait_for(predicate, *, turns: int = 100) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not met")


def test_connect_transitions_from_connecting_to_connected() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        connect_started = asyncio.Event()
        allow_connect = asyncio.Event()

        async def connector(url: str) -> FakeWebSocket:
            assert url == KRAKEN_WEBSOCKET_V2_URL
            connect_started.set()
            await allow_connect.wait()
            return websocket

        client = KrakenWebSocketV2(connector=connector)
        connect_task = asyncio.create_task(client.connect())

        await connect_started.wait()
        assert client.state is ConnectionState.CONNECTING

        allow_connect.set()
        await connect_task

        assert client.state is ConnectionState.CONNECTED
        await client.disconnect()

    asyncio.run(scenario())


def test_disconnect_closes_connection_and_marks_disconnected() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        client = KrakenWebSocketV2(connector=ConnectorScript(websocket))

        await client.connect()
        await client.disconnect()

        assert websocket.close_calls == 1
        assert client.state is ConnectionState.DISCONNECTED

    asyncio.run(scenario())


def test_ping_message_triggers_pong_response() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket()
        client = KrakenWebSocketV2(connector=ConnectorScript(websocket))

        await client.connect()
        websocket.queue_message(json.dumps({"method": "ping", "req_id": 7}))

        await _wait_for(lambda: len(websocket.sent_messages) == 1)

        assert json.loads(websocket.sent_messages[0]) == {"method": "pong", "req_id": 7}
        await client.disconnect()

    asyncio.run(scenario())


def test_reconnect_uses_exponential_backoff_and_restores_connected_state() -> None:
    async def scenario() -> None:
        first_socket = FakeWebSocket()
        second_socket = FakeWebSocket()
        connector = ConnectorScript(
            first_socket,
            ConnectionDropError("retry-1"),
            ConnectionDropError("retry-2"),
            ConnectionDropError("retry-3"),
            ConnectionDropError("retry-4"),
            ConnectionDropError("retry-5"),
            second_socket,
        )
        sleep = SleepRecorder()
        client = KrakenWebSocketV2(connector=connector, sleep=sleep)

        await client.connect()
        assert client.state is ConnectionState.CONNECTED

        first_socket.queue_message(ConnectionDropError("dropped"))

        await _wait_for(lambda: client.state is ConnectionState.RECONNECTING)
        await _wait_for(
            lambda: client.state is ConnectionState.CONNECTED and connector.call_count == 7
        )

        assert sleep.delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
        await client.disconnect()

    asyncio.run(scenario())
