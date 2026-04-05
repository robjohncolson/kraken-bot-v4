"""Kraken WebSocket v2 connection manager with reconnect support."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Protocol

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

if TYPE_CHECKING:
    from exchange.models import KrakenTrade

KRAKEN_WEBSOCKET_V2_URL = "wss://ws.kraken.com/v2"


class SupportsWebSocket(Protocol):
    async def recv(self) -> str | bytes: ...
    async def send(self, message: str) -> None: ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[SupportsWebSocket]]
Sleeper = Callable[[float], Awaitable[None]]
PriceTickHandler = Callable[[PriceTick], Awaitable[None]]
FillConfirmedHandler = Callable[[FillConfirmed], Awaitable[None]]
TickerPoller = Callable[[tuple[str, ...]], Awaitable[Iterable[PriceTick]]]
OpenOrdersPoller = Callable[[], Awaitable[Iterable["PolledOrderSnapshot"]]]
TradeHistoryPoller = Callable[[], Awaitable[Iterable["KrakenTrade"]]]


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


@dataclass(frozen=True, slots=True)
class PolledOrderSnapshot:
    order_id: str
    pair: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal = Decimal("0")
    client_order_id: str | None = None


class ReconnectPolicy:
    __slots__ = ("base_delay_sec", "max_delay_sec")

    def __init__(
        self, base_delay_sec: float = 2.0, max_delay_sec: float = 30.0
    ) -> None:
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
    error_types: list[type[BaseException]] = [
        OSError,
        TimeoutError,
        asyncio.CancelledError,
    ]
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


class FallbackPoller:
    """Poll REST snapshots while the WebSocket feed is unavailable."""

    def __init__(
        self,
        *,
        ticker_fetcher: TickerPoller | None,
        open_orders_fetcher: OpenOrdersPoller | None,
        trade_history_fetcher: TradeHistoryPoller | None = None,
        ticker_pairs: Callable[[], tuple[str, ...]],
        emit_price_tick: PriceTickHandler,
        emit_fill: FillConfirmedHandler,
        sleep: Sleeper = asyncio.sleep,
        utc_now: UtcNow = _default_utc_now,
        price_poll_interval_sec: float = 15.0,
        order_poll_interval_sec: float = 30.0,
    ) -> None:
        self._ticker_fetcher = ticker_fetcher
        self._open_orders_fetcher = open_orders_fetcher
        self._trade_history_fetcher = trade_history_fetcher
        self._ticker_pairs = ticker_pairs
        self._emit_price_tick = emit_price_tick
        self._emit_fill = emit_fill
        self._sleep = sleep
        self._utc_now = utc_now
        self._price_poll_interval_sec = price_poll_interval_sec
        self._order_poll_interval_sec = order_poll_interval_sec
        self._tasks: tuple[asyncio.Task[None], ...] = ()
        self._known_orders: dict[str, PolledOrderSnapshot] = {}

    @property
    def active(self) -> bool:
        return any(not task.done() for task in self._tasks)

    @property
    def price_poll_interval_sec(self) -> float:
        return self._price_poll_interval_sec

    @property
    def order_poll_interval_sec(self) -> float:
        return self._order_poll_interval_sec

    def activate(self) -> None:
        if self.active:
            return
        tasks: list[asyncio.Task[None]] = []
        if self._ticker_fetcher is not None:
            tasks.append(
                asyncio.create_task(
                    self._run_ticker_loop(), name="kraken-rest-fallback-ticker"
                )
            )
        if self._open_orders_fetcher is not None:
            tasks.append(
                asyncio.create_task(
                    self._run_order_loop(), name="kraken-rest-fallback-orders"
                )
            )
        self._tasks = tuple(tasks)

    async def deactivate(self) -> None:
        tasks = self._tasks
        self._tasks = ()
        self._known_orders = {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run_ticker_loop(self) -> None:
        assert self._ticker_fetcher is not None
        while True:
            pairs = self._ticker_pairs()
            if pairs:
                for tick in tuple(await self._ticker_fetcher(pairs)):
                    await self._emit_price_tick(tick)
            await self._sleep(self._price_poll_interval_sec)

    async def _run_order_loop(self) -> None:
        assert self._open_orders_fetcher is not None
        while True:
            current_orders = {
                snapshot.order_id: snapshot
                for snapshot in tuple(await self._open_orders_fetcher())
            }
            for order_id, snapshot in tuple(self._known_orders.items()):
                if order_id not in current_orders:
                    # While the private feed is unavailable, a missing open order
                    # is the only terminal signal available to downstream consumers.
                    fill = await self._resolve_terminal_fill(snapshot)
                    await self._emit_fill(fill)
            self._known_orders = current_orders
            await self._sleep(self._order_poll_interval_sec)

    async def _resolve_terminal_fill(
        self, snapshot: PolledOrderSnapshot
    ) -> FillConfirmed:
        if self._trade_history_fetcher is None:
            return self._snapshot_fill(snapshot)

        trades = tuple(await self._trade_history_fetcher())
        matches = tuple(
            trade
            for trade in trades
            if trade.order_id == snapshot.order_id
            or (
                snapshot.client_order_id is not None
                and trade.client_order_id == snapshot.client_order_id
            )
        )
        total_quantity = sum((trade.quantity for trade in matches), start=Decimal("0"))
        if total_quantity <= Decimal("0"):
            return self._snapshot_fill(snapshot)

        notional = sum(
            (trade.quantity * trade.price for trade in matches), start=Decimal("0")
        )
        total_fee = sum((trade.fee for trade in matches), start=Decimal("0"))
        timestamp = max(
            (trade.filled_at for trade in matches if trade.filled_at is not None),
            default=self._utc_now(),
        )
        side = next(
            (trade.side for trade in reversed(matches) if trade.side), snapshot.side
        )
        return FillConfirmed(
            order_id=snapshot.order_id,
            client_order_id=snapshot.client_order_id,
            pair=snapshot.pair,
            side=side,
            quantity=total_quantity,
            price=notional / total_quantity,
            fee=total_fee,
            timestamp=timestamp,
        )

    def _snapshot_fill(self, snapshot: PolledOrderSnapshot) -> FillConfirmed:
        return FillConfirmed(
            order_id=snapshot.order_id,
            client_order_id=snapshot.client_order_id,
            pair=snapshot.pair,
            side=snapshot.side,
            quantity=snapshot.quantity,
            price=snapshot.price,
            fee=snapshot.fee,
            timestamp=self._utc_now(),
        )


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
        rest_ticker_poller: TickerPoller | None = None,
        rest_open_orders_poller: OpenOrdersPoller | None = None,
        rest_trade_history_poller: TradeHistoryPoller | None = None,
        price_poll_interval_sec: float = 15.0,
        order_poll_interval_sec: float = 30.0,
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
        self._execution_token: str | None = None
        self._fallback_poller = FallbackPoller(
            ticker_fetcher=rest_ticker_poller,
            open_orders_fetcher=rest_open_orders_poller,
            trade_history_fetcher=rest_trade_history_poller,
            ticker_pairs=lambda: self._ticker_pairs,
            emit_price_tick=self._emit_price_tick,
            emit_fill=self._emit_fill_confirmed,
            sleep=self._sleep,
            utc_now=self._utc_now,
            price_poll_interval_sec=price_poll_interval_sec,
            order_poll_interval_sec=order_poll_interval_sec,
        )

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def price_ticks(self) -> asyncio.Queue[PriceTick]:
        return self._price_ticks

    @property
    def fills(self) -> asyncio.Queue[FillConfirmed]:
        return self._fills

    @property
    def fallback_poller(self) -> FallbackPoller:
        return self._fallback_poller

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
            await self._set_state(ConnectionState.DISCONNECTED)
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
        await self._set_state(ConnectionState.DISCONNECTED)

    async def subscribe_ticker(self, pairs: Iterable[str]) -> None:
        normalized = _normalize_pairs(pairs)
        if not normalized:
            raise KrakenWebSocketSubscriptionError(
                "Ticker subscription requires at least one pair."
            )
        if self._websocket is None:
            raise KrakenWebSocketSubscriptionError(
                "Cannot subscribe to ticker while disconnected."
            )
        merged = list(self._ticker_pairs)
        for p in normalized:
            if p not in merged:
                merged.append(p)
        self._ticker_pairs = tuple(merged)
        await self._send_json(
            {
                "method": "subscribe",
                "params": {"channel": "ticker", "symbol": list(normalized)},
            }
        )

    async def subscribe_executions(self, token: str) -> None:
        if self._websocket is None:
            raise KrakenWebSocketSubscriptionError(
                "Cannot subscribe to executions while disconnected."
            )
        normalized_token = token.strip()
        if not normalized_token:
            raise KrakenWebSocketSubscriptionError(
                "Execution subscription requires a WebSocket token."
            )
        self._execution_token = normalized_token
        await self._send_json(
            {
                "method": "subscribe",
                "params": {"channel": "executions", "token": normalized_token},
            }
        )

    # --- internal ---

    async def _run(self) -> None:
        while self._should_reconnect:
            ws = self._websocket
            if ws is None:
                await self._set_state(ConnectionState.DISCONNECTED)
                return
            try:
                raw = await ws.recv()
                await self._handle_message(raw)
            except asyncio.CancelledError:
                return
            except self._disconnect_errors:
                self._websocket = None
                await self._set_state(ConnectionState.DISCONNECTED)
                if not self._should_reconnect:
                    return
                await self._reconnect()

    async def _reconnect(self) -> None:
        while self._should_reconnect:
            await self._set_state(ConnectionState.RECONNECTING)
            await self._sleep(self._next_delay())
            try:
                await self._open_connection(ConnectionState.RECONNECTING)
            except KrakenWebSocketConnectError:
                continue
            return
        await self._set_state(ConnectionState.DISCONNECTED)

    async def _open_connection(self, state: ConnectionState) -> None:
        await self._set_state(state)
        try:
            ws = await self._connector(self._url)
        except KrakenWebSocketConnectError:
            raise
        except self._connect_errors as exc:
            raise KrakenWebSocketConnectError(self._url) from exc
        self._websocket = ws
        self._reconnect_attempt = 0
        await self._restore_subscriptions()
        await self._set_state(ConnectionState.CONNECTED)

    def _next_delay(self) -> float:
        delay = min(
            self._reconnect_policy.base_delay_sec * (2**self._reconnect_attempt),
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
            await self._emit_price_tick(tick)
        for fill in parse_execution_payload(payload, utc_now=self._utc_now):
            await self._emit_fill_confirmed(fill)

    async def _emit_price_tick(self, tick: PriceTick) -> None:
        await self._price_ticks.put(tick)
        if self._ticker_handler is not None:
            await self._ticker_handler(tick)

    async def _emit_fill_confirmed(self, fill: FillConfirmed) -> None:
        await self._fills.put(fill)
        if self._fill_handler is not None:
            await self._fill_handler(fill)

    async def _restore_subscriptions(self) -> None:
        if self._ticker_pairs:
            await self._send_json(
                {
                    "method": "subscribe",
                    "params": {"channel": "ticker", "symbol": list(self._ticker_pairs)},
                }
            )
        if self._execution_token is not None:
            await self._send_json(
                {
                    "method": "subscribe",
                    "params": {"channel": "executions", "token": self._execution_token},
                }
            )

    async def _set_state(self, state: ConnectionState) -> None:
        self._state = state
        if state is ConnectionState.CONNECTED:
            await self._fallback_poller.deactivate()
            return
        if self._should_reconnect and state in (
            ConnectionState.DISCONNECTED,
            ConnectionState.RECONNECTING,
        ):
            self._fallback_poller.activate()
            return
        await self._fallback_poller.deactivate()

    async def _send_json(self, payload: dict[str, object]) -> None:
        ws = self._websocket
        if ws is None:
            raise KrakenWebSocketSubscriptionError(
                "Cannot send WebSocket message while disconnected."
            )
        await ws.send(json.dumps(payload, separators=(",", ":")))


__all__ = [
    "ConnectionState",
    "FallbackPoller",
    "FillConfirmed",
    "KRAKEN_WEBSOCKET_V2_URL",
    "KrakenWebSocketConnectError",
    "KrakenWebSocketError",
    "KrakenWebSocketSubscriptionError",
    "KrakenWebSocketV2",
    "PolledOrderSnapshot",
    "PriceTick",
    "ReconnectPolicy",
]
