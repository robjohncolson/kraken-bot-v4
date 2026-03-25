from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.config import Settings, load_settings
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    BotState,
    MarketRegime,
    Portfolio,
    Position,
    PositionSide,
)
from exchange.models import KrakenOrder, KrakenState
from exchange.websocket import ConnectionState, FillConfirmed, PriceTick
from guardian import PriceSnapshot
from persistence.sqlite import ensure_schema
from runtime_loop import SchedulerRuntime, build_initial_scheduler_state
from scheduler import SchedulerConfig
from trading.reconciler import RecordedPosition, RecordedState, ReconciliationReport

NOW = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)


class FakeExecutor:
    def __init__(self, kraken_state: KrakenState, *, ws_token: str = "ws-token-123") -> None:
        self.kraken_state = kraken_state
        self.ws_token = ws_token
        self.fetch_calls = 0
        self.token_calls = 0

    def fetch_kraken_state(self) -> KrakenState:
        self.fetch_calls += 1
        return self.kraken_state

    def get_ws_token(self) -> str:
        self.token_calls += 1
        return self.ws_token


class FakeRuntimeWebSocket:
    def __init__(self, on_tick, on_fill) -> None:
        self._on_tick = on_tick
        self._on_fill = on_fill
        self.state = ConnectionState.DISCONNECTED
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.ticker_subscriptions: list[tuple[str, ...]] = []
        self.execution_tokens: list[str] = []

    async def connect(self) -> None:
        self.connect_calls += 1
        self.state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.state = ConnectionState.DISCONNECTED

    async def subscribe_ticker(self, pairs) -> None:
        self.ticker_subscriptions.append(tuple(pairs))

    async def subscribe_executions(self, token: str) -> None:
        self.execution_tokens.append(token)

    async def emit_tick(self, tick: PriceTick) -> None:
        await self._on_tick(tick)

    async def emit_fill(self, fill: FillConfirmed) -> None:
        await self._on_fill(fill)


def _settings() -> Settings:
    return load_settings(
        {
            "KRAKEN_API_KEY": "key",
            "KRAKEN_API_SECRET": "secret",
            "WEB_PORT": "8081",
        }
    )


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def _position(pair: str = "DOGE/USD") -> Position:
    return Position(
        position_id="pos-1",
        pair=pair,
        side=PositionSide.LONG,
        quantity=Decimal("100"),
        entry_price=Decimal("0.12"),
        stop_price=Decimal("0.10"),
        target_price=Decimal("0.20"),
    )


def test_start_connects_websocket_subscribes_and_publishes_dashboard_state() -> None:
    async def scenario() -> None:
        published: list[dict[str, object]] = []
        heartbeats = []
        fake_websocket: FakeRuntimeWebSocket | None = None

        async def capture_publish(*, event: str, data, event_id: str | None = None) -> None:
            published.append({"event": event, "data": data, "event_id": event_id})

        def websocket_factory(on_tick, on_fill) -> FakeRuntimeWebSocket:
            nonlocal fake_websocket
            fake_websocket = FakeRuntimeWebSocket(on_tick, on_fill)
            return fake_websocket

        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(KrakenState()),
            conn=_memory_db(),
            initial_state=build_initial_scheduler_state(
                kraken_state=KrakenState(),
                recorded_state=RecordedState(
                    positions=(RecordedPosition(position_id="pos-1", pair="DOGE/USD"),),
                ),
                report=ReconciliationReport(),
                now=NOW,
            ),
            websocket_factory=websocket_factory,
            serve_dashboard=False,
            sse_publisher=capture_publish,
            heartbeat_writer=heartbeats.append,
            utc_now=lambda: NOW,
        )

        await runtime.start()
        assert fake_websocket is not None
        assert fake_websocket.connect_calls == 1
        assert fake_websocket.ticker_subscriptions == [("DOGE/USD",)]
        assert fake_websocket.execution_tokens == ["ws-token-123"]
        assert published[0]["event"] == "dashboard.update"
        assert set(published[0]["data"]) == {
            "portfolio",
            "positions",
            "grid",
            "beliefs",
            "stats",
            "reconciliation",
        }
        assert heartbeats[0].websocket_connected is True
        await runtime.shutdown()

    asyncio.run(scenario())


def test_fill_confirmed_writes_ledger_and_refreshes_runtime_state() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        refreshed = KrakenState(
            open_orders=(
                KrakenOrder(
                    order_id="order-2",
                    pair="DOGE/USD",
                    client_order_id="kbv4-dogeusd-000002",
                    opened_at=NOW,
                ),
            ),
        )
        fake_websocket: FakeRuntimeWebSocket | None = None

        def websocket_factory(on_tick, on_fill) -> FakeRuntimeWebSocket:
            nonlocal fake_websocket
            fake_websocket = FakeRuntimeWebSocket(on_tick, on_fill)
            return fake_websocket

        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(refreshed),
            conn=conn,
            initial_state=build_initial_scheduler_state(
                kraken_state=KrakenState(),
                recorded_state=RecordedState(),
                report=ReconciliationReport(),
                now=NOW,
            ),
            websocket_factory=websocket_factory,
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        assert fake_websocket is not None
        fill = FillConfirmed(
            order_id="order-1",
            client_order_id="kbv4-dogeusd-000001",
            pair="DOGE/USD",
            side="buy",
            quantity=Decimal("125"),
            price=Decimal("0.1234"),
            fee=Decimal("0.05"),
            timestamp=NOW,
        )
        await fake_websocket.emit_fill(fill)

        rows = conn.execute(
            "SELECT pair, side, quantity, price, fee, filled_at FROM ledger ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["pair"] == "DOGE/USD"
        assert rows[0]["side"] == "buy"
        assert rows[0]["quantity"] == "125"
        assert rows[0]["price"] == "0.1234"
        assert rows[0]["fee"] == "0.05"
        assert rows[0]["filled_at"] == "2026-03-25T12:00:00Z"
        assert runtime.state.kraken_state == refreshed
        assert runtime.state.last_reconcile_at is None

    asyncio.run(scenario())


def test_belief_refresh_handler_enqueues_and_applies_belief_on_next_cycle() -> None:
    async def scenario() -> None:
        fake_websocket: FakeRuntimeWebSocket | None = None
        fresh_belief = BeliefSnapshot(
            pair="DOGE/USD",
            direction=BeliefDirection.BULLISH,
            confidence=0.82,
            regime=MarketRegime.TRENDING,
            sources=(BeliefSource.CODEX,),
        )

        def websocket_factory(on_tick, on_fill) -> FakeRuntimeWebSocket:
            nonlocal fake_websocket
            fake_websocket = FakeRuntimeWebSocket(on_tick, on_fill)
            return fake_websocket

        def refresh_handler(_request) -> BeliefSnapshot:
            return fresh_belief

        initial_state = build_initial_scheduler_state(
            kraken_state=KrakenState(),
            recorded_state=RecordedState(),
            report=ReconciliationReport(),
            now=NOW,
        )
        initial_state = replace(
            initial_state,
            bot_state=BotState(portfolio=Portfolio(positions=(_position(),))),
            current_prices={
                "DOGE/USD": PriceSnapshot(
                    price=Decimal("0.12"),
                    belief_timestamp=NOW - timedelta(hours=5),
                )
            },
            last_guardian_check_at=None,
            last_reconcile_at=NOW,
        )

        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(KrakenState()),
            conn=_memory_db(),
            initial_state=initial_state,
            scheduler_config=SchedulerConfig(
                cycle_interval_sec=1,
                reconcile_interval_sec=9999,
                guardian_interval_sec=60,
            ),
            websocket_factory=websocket_factory,
            belief_refresh_handler=refresh_handler,
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        assert fake_websocket is not None
        await fake_websocket.emit_tick(
            PriceTick(
                pair="DOGE/USD",
                bid=Decimal("0.119"),
                ask=Decimal("0.121"),
                last=Decimal("0.12"),
                timestamp=NOW,
            )
        )

        await runtime.run_once()
        assert runtime.state.pending_belief_signals == (fresh_belief,)
        assert runtime.state.bot_state.beliefs == ()

        await runtime.run_once()
        assert runtime.state.pending_belief_signals == ()
        assert runtime.state.bot_state.beliefs == (fresh_belief,)

    asyncio.run(scenario())


async def _noop_publish(*, event: str, data, event_id: str | None = None) -> None:
    del event, data, event_id
