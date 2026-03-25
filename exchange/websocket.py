"""Kraken WebSocket v2 connection manager with reconnect support."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable
from enum import Enum
from typing import Protocol

from core.errors import ExchangeError
from exchange.symbols import normalize_pair
from exchange.ws_parsers import (
    FillConfirmed,
    PriceTick,
    UtcNow,
    build_pong_message,
    decode_message,
    is_ping_message,
    parse_execution_payload,
    parse_ticker_payload,
)

KRAKEN_WEBSOCKET_V2_URL = "wss://ws.kraken.com/v2"


class SupportsWebSocket(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[SupportsWebSocket]]
Sleeper = Callable[[float], Awaitable[None]]
PriceTickHandler = Callable[[PriceTick], Awaitable[None]]
FillConfirmedHandler = Callable[[FillConfirmed], Awaitable[None]]


class ConnectionState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    RECONNECTING = "RECONNECTING"


class KrakenWebSocketError(ExchangeError):
    """Base exception for Kraken WebSocket failures."""


class KrakenWebSocketConnectError(KrakenWebSocketError):
    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"Failed to connect to Kraken WebSocket at {url}.")


class KrakenWebSocketSubscriptionError(KrakenWebSocketError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ReconnectPolicy:
    __slots__ = ("base_delay_sec", "max_delay_sec")

    def __init__(self, base_delay_sec: float = 2.0, max_delay_sec: float = 30.0) -> None:
        self.base_delay_sec = base_delay_sec
        self.max_delay_sec = max_delay_sec


def _default_utc_now():  # noqa: ANN202
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


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
        error_types.extend([InvalidHandshake, InvalidURI])
    except ModuleNotFoundError:
        pass
    return tuple(error_types)


def _disconnect_error_types() -> tuple[type[BaseException], ...]:
    error_types: list[type[BaseException]] = [OSError, TimeoutError]
    try:
        from websockets.exceptions import ConnectionClosed
        error_types.append(ConnectionClosed)
    except ModuleNotFoundError:
        pass
    return tuple(error_types)


def _normalize_pairs(pairs: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for pair in pairs:
        p = normalize_pair(pair)
        if p not in seen:
            seen.append(p)
    return tuple(seen)


class KrakenWebSocketV2:
    """Kraken WebSocket v2 client with auto-reconnect and feed subscriptions."""

    def __init__(
        self,
        *,
        url: str = KRAKEN_WEBSOCKET_V2_URL,
        connector: Connector = _default_connector,
        sleep: Sleeper = asyncio.sleep,
        reconnect_policy: ReconnectPolicy | None = None,
        ticker_handler: PriceTickHandler | None = None,
        fill_handler: FillConfirmedHandler | None = None,
        utc_now: UtcNow = _default_utc_now,
    ) -> None:
        self._url = url
        self._connector = connector
        self._sleep = sleep
        self._reconnect_policy = reconnect_policy or ReconnectPolicy()
        self._ticker_handler = ticker_handler
        self._fill_handler = fill_handler
        self._utc_now = utc_now
        self._state = ConnectionState.DISCONNECTED
        self._websocket: SupportsWebSocket | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._should_reconnect = False
        self._reconnect_attempt = 0
        self._connect_errors = _connect_error_types()
        self._disconnect_errors = _disconnect_error_types()
        self._price_ticks: asyncio.Queue[PriceTick] = asyncio.Queue()
        self._fills: asyncio.Queue[FillConfirmed] = asyncio.Queue()
        self._ticker_pairs: tuple[str, ...] = ()

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def price_ticks(self) -> asyncio.Queue[PriceTick]:
        return self._price_ticks

    @property
    def fills(self) -> asyncio.Queue[FillConfirmed]:
        return self._fills

    async def get_price_tick(self) -> PriceTick:
        return await self._price_ticks.get()

    async def get_fill_confirmed(self) -> FillConfirmed:
        return await self._fills.get()

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

    async def subscribe_ticker(self, pairs: Iterable[str]) -> None:
        normalized = _normalize_pairs(pairs)
        if not normalized:
            raise KrakenWebSocketSubscriptionError("Ticker subscription requires at least one pair.")
        if self._websocket is None:
            raise KrakenWebSocketSubscriptionError("Cannot subscribe to ticker while disconnected.")
        merged = list(self._ticker_pairs)
        for p in normalized:
            if p not in merged:
                merged.append(p)
        self._ticker_pairs = tuple(merged)
        await self._send_json({"method": "subscribe", "params": {"channel": "ticker", "symbol": list(normalized)}})

    async def subscribe_executions(self, token: str) -> None:
        if self._websocket is None:
            raise KrakenWebSocketSubscriptionError("Cannot subscribe to executions while disconnected.")
        if not token.strip():
            raise KrakenWebSocketSubscriptionError("Execution subscription requires a WebSocket token.")
        await self._send_json({"method": "subscribe", "params": {"channel": "executions", "token": token}})

    # --- internal ---

    async def _run(self) -> None:
        while self._should_reconnect:
            ws = self._websocket
            if ws is None:
                self._state = ConnectionState.DISCONNECTED
                return
            try:
                raw = await ws.recv()
                await self._handle_message(raw)
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
            await self._sleep(self._next_delay())
            try:
                await self._open_connection(ConnectionState.RECONNECTING)
            except KrakenWebSocketConnectError:
                continue
            return
        self._state = ConnectionState.DISCONNECTED

    async def _open_connection(self, state: ConnectionState) -> None:
        self._state = state
        try:
            ws = await self._connector(self._url)
        except KrakenWebSocketConnectError:
            raise
        except self._connect_errors as exc:
            raise KrakenWebSocketConnectError(self._url) from exc
        self._websocket = ws
        self._reconnect_attempt = 0
        self._state = ConnectionState.CONNECTED

    def _next_delay(self) -> float:
        delay = min(
            self._reconnect_policy.base_delay_sec * (2 ** self._reconnect_attempt),
            self._reconnect_policy.max_delay_sec,
        )
        self._reconnect_attempt += 1
        return delay

    async def _handle_message(self, raw: str | bytes) -> None:
        payload = decode_message(raw)
        if payload is None:
            return
        if is_ping_message(payload):
            await self._send_json(build_pong_message(payload))
            return
        for tick in parse_ticker_payload(payload, utc_now=self._utc_now):
            await self._price_ticks.put(tick)
            if self._ticker_handler is not None:
                await self._ticker_handler(tick)
        for fill in parse_execution_payload(payload, utc_now=self._utc_now):
            await self._fills.put(fill)
            if self._fill_handler is not None:
                await self._fill_handler(fill)

    async def _send_json(self, payload: dict[str, object]) -> None:
        ws = self._websocket
        if ws is None:
            raise KrakenWebSocketSubscriptionError("Cannot send WebSocket message while disconnected.")
        await ws.send(json.dumps(payload, separators=(",", ":")))


__all__ = [
    "ConnectionState",
    "FillConfirmed",
    "KRAKEN_WEBSOCKET_V2_URL",
    "KrakenWebSocketConnectError",
    "KrakenWebSocketError",
    "KrakenWebSocketSubscriptionError",
    "KrakenWebSocketV2",
    "PriceTick",
    "ReconnectPolicy",
]
