from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Protocol

from core.errors import ExchangeError
from exchange.symbols import SymbolNormalizationError, normalize_pair

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
PriceTickHandler = Callable[["PriceTick"], Awaitable[None]]
UtcNow = Callable[[], datetime]


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


class KrakenWebSocketSubscriptionError(KrakenWebSocketError):
    """Raised when a WebSocket subscription cannot be sent."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    base_delay_sec: float = 2.0
    max_delay_sec: float = 30.0


@dataclass(frozen=True, slots=True)
class PriceTick:
    pair: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    timestamp: datetime


async def _default_connector(url: str) -> SupportsWebSocket:
    try:
        import websockets
    except ModuleNotFoundError as exc:
        raise KrakenWebSocketConnectError(url) from exc

    return await websockets.connect(url)


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
        ticker_handler: PriceTickHandler | None = None,
        utc_now: UtcNow = _default_utc_now,
    ) -> None:
        self._url = url
        self._connector = connector
        self._sleep = sleep
        self._reconnect_policy = reconnect_policy
        self._ticker_handler = ticker_handler
        self._utc_now = utc_now
        self._state = ConnectionState.DISCONNECTED
        self._websocket: SupportsWebSocket | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._should_reconnect = False
        self._reconnect_attempt = 0
        self._connect_errors = _connect_error_types()
        self._disconnect_errors = _disconnect_error_types()
        self._price_ticks: asyncio.Queue[PriceTick] = asyncio.Queue()
        self._ticker_pairs: tuple[str, ...] = ()

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def price_ticks(self) -> asyncio.Queue[PriceTick]:
        return self._price_ticks

    async def get_price_tick(self) -> PriceTick:
        return await self._price_ticks.get()

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
        normalized_pairs = _normalize_pairs(pairs)
        if not normalized_pairs:
            raise KrakenWebSocketSubscriptionError(
                "Ticker subscription requires at least one pair."
            )

        if self._websocket is None:
            raise KrakenWebSocketSubscriptionError(
                "Cannot subscribe to ticker while disconnected."
            )

        self._ticker_pairs = _merge_pairs(self._ticker_pairs, normalized_pairs)
        await self._send_ticker_subscription(normalized_pairs)

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
        if payload is None:
            return

        if _is_ping_message(payload):
            await self._send_json(_build_pong_message(payload))
            return

        for tick in _parse_ticker_payload(payload, utc_now=self._utc_now):
            await self._emit_price_tick(tick)

    async def _send_ticker_subscription(self, pairs: tuple[str, ...]) -> None:
        await self._send_json(
            {
                "method": "subscribe",
                "params": {
                    "channel": "ticker",
                    "symbol": list(pairs),
                },
            }
        )

    async def _send_json(self, payload: dict[str, object]) -> None:
        websocket = self._websocket
        if websocket is None:
            raise KrakenWebSocketSubscriptionError(
                "Cannot send WebSocket message while disconnected."
            )
        await websocket.send(json.dumps(payload, separators=(",", ":")))

    async def _emit_price_tick(self, tick: PriceTick) -> None:
        await self._price_ticks.put(tick)
        if self._ticker_handler is not None:
            await self._ticker_handler(tick)


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


def _normalize_pairs(pairs: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for pair in pairs:
        normalized_pair = normalize_pair(pair)
        if normalized_pair not in normalized:
            normalized.append(normalized_pair)
    return tuple(normalized)


def _merge_pairs(existing: tuple[str, ...], new_pairs: tuple[str, ...]) -> tuple[str, ...]:
    merged = list(existing)
    for pair in new_pairs:
        if pair not in merged:
            merged.append(pair)
    return tuple(merged)


def _parse_ticker_payload(
    payload: Mapping[str, object], *, utc_now: UtcNow
) -> tuple[PriceTick, ...]:
    if payload.get("channel") != "ticker":
        return ()

    raw_data = payload.get("data")
    if not isinstance(raw_data, (list, tuple)):
        return ()

    message_timestamp = _parse_timestamp(_first_present(payload, "timestamp", "time_in", "time_out"))
    ticks: list[PriceTick] = []
    for entry in raw_data:
        if not isinstance(entry, Mapping):
            continue

        raw_symbol = entry.get("symbol")
        if not isinstance(raw_symbol, str):
            continue

        try:
            pair = normalize_pair(raw_symbol)
        except SymbolNormalizationError:
            continue

        bid = _extract_decimal(_first_present(entry, "bid", "best_bid"))
        ask = _extract_decimal(_first_present(entry, "ask", "best_ask"))
        last = _extract_decimal(_first_present(entry, "last", "last_price"))
        if bid is None or ask is None or last is None:
            continue

        timestamp = _parse_timestamp(_first_present(entry, "timestamp", "time", "as_of"))
        if timestamp is None:
            timestamp = message_timestamp
        if timestamp is None:
            timestamp = utc_now()

        ticks.append(
            PriceTick(
                pair=pair,
                bid=bid,
                ask=ask,
                last=last,
                timestamp=timestamp,
            )
        )

    return tuple(ticks)


def _first_present(mapping: Mapping[str, object], *keys: str) -> object | None:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _extract_decimal(value: object) -> Decimal | None:
    raw_value = value
    if isinstance(value, Mapping):
        raw_value = _first_present(value, "price", "value")
    if raw_value is None:
        return None

    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_timestamp(value: object) -> datetime | None:
    if value is None or isinstance(value, bool):
        return None

    if isinstance(value, datetime):
        return _to_utc(value)

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    if not isinstance(value, str):
        return None

    stripped = value.strip()
    if not stripped:
        return None

    try:
        return _to_utc(datetime.fromisoformat(stripped.replace("Z", "+00:00")))
    except ValueError:
        try:
            return datetime.fromtimestamp(float(stripped), tz=timezone.utc)
        except ValueError:
            return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


__all__ = [
    "ConnectionState",
    "KRAKEN_WEBSOCKET_V2_URL",
    "KrakenWebSocketConnectError",
    "KrakenWebSocketError",
    "KrakenWebSocketSubscriptionError",
    "KrakenWebSocketV2",
    "PriceTick",
    "ReconnectPolicy",
]
