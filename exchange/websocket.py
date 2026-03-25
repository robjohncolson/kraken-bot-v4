from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from core.errors import ExchangeError

KRAKEN_WEBSOCKET_V2_URL = "wss://ws.kraken.com/v2"


class SupportsWebSocket(Protocol):
    async def recv(self) -> str | bytes:
        """Receive the next WebSocket frame."""

    async def send(self, message: str) -> None:
        """Send a text WebSocket frame."""

    async def close(self) -> None:
        """Close the connection."""


Connector = Callable[[str], Awaitable[SupportsWebSocket]]
Sleeper = Callable[[float], Awaitable[None]]


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"


class KrakenWebSocketError(ExchangeError):
    """Base exception for Kraken WebSocket failures."""


class KrakenWebSocketConnectError(KrakenWebSocketError):
    """Raised when the Kraken WebSocket cannot be established."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Failed to connect to Kraken WebSocket at {url}.")


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    base_delay_sec: float = 2.0
    max_delay_sec: float = 30.0


async def _default_connector(url: str) -> SupportsWebSocket:
    try:
        import websockets
    except ModuleNotFoundError as exc:
        raise KrakenWebSocketConnectError(url) from exc

    return await websockets.connect(url)


def _connect_error_types() -> tuple[type[BaseException], ...]:
    error_types: list[type[BaseException]] = [OSError, TimeoutError]
    try:
        from websockets.exceptions import InvalidHandshake, InvalidURI
    except ModuleNotFoundError:
        return tuple(error_types)

    return tuple([*error_types, InvalidHandshake, InvalidURI])


def _disconnect_error_types() -> tuple[type[BaseException], ...]:
    error_types: list[type[BaseException]] = [OSError, TimeoutError]
    try:
        from websockets.exceptions import ConnectionClosed
    except ModuleNotFoundError:
        return tuple(error_types)

    return tuple([*error_types, ConnectionClosed])


class KrakenWebSocketV2:
    """Minimal Kraken WebSocket v2 connection manager with reconnect support."""

    def __init__(
        self,
        *,
        url: str = KRAKEN_WEBSOCKET_V2_URL,
        connector: Connector = _default_connector,
        sleep: Sleeper = asyncio.sleep,
        reconnect_policy: ReconnectPolicy = ReconnectPolicy(),
    ) -> None:
        self._url = url
        self._connector = connector
        self._sleep = sleep
        self._reconnect_policy = reconnect_policy
        self._state = ConnectionState.DISCONNECTED
        self._websocket: SupportsWebSocket | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._should_reconnect = False
        self._reconnect_attempt = 0
        self._connect_errors = _connect_error_types()
        self._disconnect_errors = _disconnect_error_types()

    @property
    def state(self) -> ConnectionState:
        return self._state

    async def connect(self) -> None:
        if self._state is not ConnectionState.DISCONNECTED:
            return

        self._should_reconnect = True
        try:
            await self._open_connection(ConnectionState.CONNECTING)
        except KrakenWebSocketConnectError:
            self._should_reconnect = False
            self._state = ConnectionState.DISCONNECTED
            raise

        self._reader_task = asyncio.create_task(self._run(), name="kraken-websocket-v2")

    async def disconnect(self) -> None:
        self._should_reconnect = False

        reader_task = self._reader_task
        self._reader_task = None
        if reader_task is not None:
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task

        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            await websocket.close()

        self._state = ConnectionState.DISCONNECTED

    async def _run(self) -> None:
        while self._should_reconnect:
            websocket = self._websocket
            if websocket is None:
                self._state = ConnectionState.DISCONNECTED
                return

            try:
                raw_message = await websocket.recv()
                await self._handle_message(raw_message)
            except asyncio.CancelledError:
                return
            except self._disconnect_errors:
                self._websocket = None
                if not self._should_reconnect:
                    self._state = ConnectionState.DISCONNECTED
                    return
                await self._reconnect()

    async def _reconnect(self) -> None:
        while self._should_reconnect:
            self._state = ConnectionState.RECONNECTING
            await self._sleep(self._next_reconnect_delay())
            try:
                await self._open_connection(ConnectionState.RECONNECTING)
            except KrakenWebSocketConnectError:
                continue
            return

        self._state = ConnectionState.DISCONNECTED

    async def _open_connection(self, state: ConnectionState) -> None:
        self._state = state
        try:
            websocket = await self._connector(self._url)
        except KrakenWebSocketConnectError:
            raise
        except self._connect_errors as exc:
            raise KrakenWebSocketConnectError(self._url) from exc

        self._websocket = websocket
        self._reconnect_attempt = 0
        self._state = ConnectionState.CONNECTED

    def _next_reconnect_delay(self) -> float:
        delay = min(
            self._reconnect_policy.base_delay_sec * (2**self._reconnect_attempt),
            self._reconnect_policy.max_delay_sec,
        )
        self._reconnect_attempt += 1
        return delay

    async def _handle_message(self, raw_message: str | bytes) -> None:
        payload = _decode_message(raw_message)
        if payload is None or not _is_ping_message(payload):
            return

        websocket = self._websocket
        if websocket is None:
            return

        await websocket.send(json.dumps(_build_pong_message(payload), separators=(",", ":")))


def _decode_message(raw_message: str | bytes) -> dict[str, object] | None:
    if isinstance(raw_message, bytes):
        message_text = raw_message.decode("utf-8")
    else:
        message_text = raw_message

    try:
        payload = json.loads(message_text)
    except json.JSONDecodeError:
        return None

    if isinstance(payload, dict):
        return payload
    return None


def _is_ping_message(payload: dict[str, object]) -> bool:
    return (
        payload.get("method") == "ping"
        or payload.get("event") == "ping"
        or payload.get("type") == "ping"
        or payload.get("channel") == "ping"
    )


def _build_pong_message(payload: dict[str, object]) -> dict[str, object]:
    if payload.get("event") == "ping":
        pong: dict[str, object] = {"event": "pong"}
    elif payload.get("type") == "ping":
        pong = {"type": "pong"}
    elif payload.get("channel") == "ping":
        pong = {"channel": "pong"}
    else:
        pong = {"method": "pong"}

    if "req_id" in payload:
        pong["req_id"] = payload["req_id"]
    return pong


__all__ = [
    "ConnectionState",
    "KRAKEN_WEBSOCKET_V2_URL",
    "KrakenWebSocketConnectError",
    "KrakenWebSocketError",
    "KrakenWebSocketV2",
    "ReconnectPolicy",
]
