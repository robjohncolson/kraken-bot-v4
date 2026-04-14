from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pandas as pd
from unittest.mock import AsyncMock, patch

from core.config import Settings, load_settings
from core.types import (
    Balance,
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    BotState,
    CancelOrder,
    LogEvent,
    MarketRegime,
    OrderSide,
    OrderType,
    PendingOrder,
    Portfolio,
    Position,
    PositionSide,
    RotationExitReason,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
    ZERO_DECIMAL,
)
from exchange.executor import CancelOrderNotFoundError
from exchange.models import KrakenOrder, KrakenState, KrakenTrade
from exchange.websocket import ConnectionState, FillConfirmed, PriceTick
from guardian import MissingCurrentPriceError, PriceSnapshot
from persistence.sqlite import SqliteReader, SqliteWriter, ensure_schema
from runtime_loop import (
    ROTATION_TREE_DRIFT_DEDUPE_WINDOW,
    RotationTreeRootValue,
    RotationTreeValueResult,
    SchedulerRuntime,
    build_initial_scheduler_state,
)
from scheduler import ReconciliationDiscrepancy, SchedulerConfig
from trading.conditional_tree import ConditionalTreeState
from trading.reconciler import (
    ReconciliationAction,
    ReconciliationReport,
    ReconciliationSeverity,
    RecordedPosition,
    RecordedState,
    UntrackedAsset,
)

NOW = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)


class FakeExecutor:
    def __init__(
        self, kraken_state: KrakenState, *, ws_token: str = "ws-token-123"
    ) -> None:
        self.kraken_state = kraken_state
        self.ws_token = ws_token
        self.fetch_calls = 0
        self.token_calls = 0
        self.cancel_calls: list[str] = []
        self.order_calls = 0
        self._client = object()

    def fetch_kraken_state(self) -> KrakenState:
        self.fetch_calls += 1
        return self.kraken_state

    def fetch_open_orders(self) -> tuple[KrakenOrder, ...]:
        return self.kraken_state.open_orders

    def fetch_trade_history(self) -> tuple[KrakenTrade, ...]:
        return self.kraken_state.trade_history

    def get_ws_token(self) -> str:
        self.token_calls += 1
        return self.ws_token

    def execute_cancel(self, order_id: str) -> int:
        self.cancel_calls.append(order_id)
        return 1

    def execute_order(self, _order) -> str:
        self.order_calls += 1
        return f"order-{self.order_calls}"


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


def _settings(**overrides: str) -> Settings:
    return load_settings(
        {
            "KRAKEN_API_KEY": "key",
            "KRAKEN_API_SECRET": "secret",
            "WEB_PORT": "8081",
            **overrides,
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


def _runtime(
    *,
    settings: Settings | None = None,
    conn: sqlite3.Connection | None = None,
    executor: FakeExecutor | None = None,
) -> SchedulerRuntime:
    runtime_conn = _memory_db() if conn is None else conn
    runtime_executor = FakeExecutor(KrakenState()) if executor is None else executor
    return SchedulerRuntime(
        settings=_settings() if settings is None else settings,
        executor=runtime_executor,
        conn=runtime_conn,
        initial_state=build_initial_scheduler_state(
            kraken_state=runtime_executor.kraken_state,
            recorded_state=RecordedState(),
            report=ReconciliationReport(),
            now=NOW,
        ),
        websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
            on_tick, on_fill
        ),
        serve_dashboard=False,
        sse_publisher=_noop_publish,
        heartbeat_writer=lambda snapshot: None,
        utc_now=lambda: NOW,
    )


def _sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _insert_open_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    created_at: datetime,
    kind: str = "cc_api",
    pair: str = "PEPE/USD",
    side: str = "sell",
    base_qty: Decimal = Decimal("10348231.25"),
    limit_price: Decimal | None = Decimal("0.000003512"),
) -> None:
    writer = SqliteWriter(conn)
    writer.upsert_order(
        order_id,
        pair,
        f"kbv4-cc-{order_id}",
        kind=kind,
        side=side,
        base_qty=base_qty,
        filled_qty=ZERO_DECIMAL,
        quote_qty=ZERO_DECIMAL,
        limit_price=limit_price,
        exchange_order_id=order_id,
    )
    conn.execute(
        "UPDATE orders SET created_at = ? WHERE order_id = ?",
        (_sqlite_timestamp(created_at), order_id),
    )
    conn.commit()


def _reconciliation_report_with_untracked_assets(
    *symbols: str,
) -> ReconciliationReport:
    return ReconciliationReport(
        untracked_assets=tuple(
            UntrackedAsset(
                asset=symbol,
                available=Decimal("1"),
                held=Decimal("0"),
                severity=ReconciliationSeverity.HIGH,
                recommended_action=ReconciliationAction.ALERT,
            )
            for symbol in symbols
        )
    )


def _bars_with_closes(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [price * 1.01 for price in closes],
            "low": [price * 0.99 for price in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


class FakeRootPairScanner:
    def __init__(
        self,
        bars: pd.DataFrame,
        *,
        pairs: tuple[tuple[str, str, str], ...],
    ) -> None:
        self._bars = bars
        self._pairs = pairs

    def discover_asset_pairs(self, _asset: str) -> tuple[tuple[str, str, str], ...]:
        return self._pairs

    def _ohlcv_fetcher(self, _pair: str, **_kwargs) -> pd.DataFrame:
        return self._bars


def test_start_connects_websocket_subscribes_and_publishes_dashboard_state() -> None:
    async def scenario() -> None:
        published: list[dict[str, object]] = []
        heartbeats = []
        fake_websocket: FakeRuntimeWebSocket | None = None

        async def capture_publish(
            *, event: str, data, event_id: str | None = None
        ) -> None:
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
        # WS connect is deferred to run_once (skipped in start for network resilience)
        assert fake_websocket.connect_calls == 0
        assert published[0]["event"] == "dashboard.update"
        assert set(published[0]["data"]) == {
            "health",
            "portfolio",
            "positions",
            "grid",
            "beliefs",
            "stats",
            "reconciliation",
            "rotation_tree",
            "pending_orders",
            "rotation_events",
        }
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


def test_run_once_seeds_candidate_price_subscribes_pair_and_enqueues_rotation_belief() -> (
    None
):
    async def scenario() -> None:
        fake_websocket: FakeRuntimeWebSocket | None = None
        candidate_belief = BeliefSnapshot(
            pair="BTC/USD",
            direction=BeliefDirection.BULLISH,
            confidence=0.88,
            regime=MarketRegime.TRENDING,
            sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
        )

        class FakeConditionalTree:
            def __init__(self) -> None:
                self.calls = 0

            def maybe_plan(self, *, state, tree_state, now):
                del state, tree_state
                self.calls += 1
                return ConditionalTreeState(
                    is_active=True,
                    trigger_time=now,
                    bear_estimate=None,
                    chosen_candidate=type(
                        "Candidate",
                        (),
                        {
                            "pair": "BTC/USD",
                            "belief": candidate_belief,
                            "reference_price_hint": Decimal("101.25"),
                            "estimated_peak_hours": 6,
                        },
                    )(),
                    exit_deadline=now + timedelta(hours=6),
                )

        conditional_tree = FakeConditionalTree()

        def websocket_factory(on_tick, on_fill) -> FakeRuntimeWebSocket:
            nonlocal fake_websocket
            fake_websocket = FakeRuntimeWebSocket(on_tick, on_fill)
            fake_websocket.state = ConnectionState.CONNECTED
            return fake_websocket

        initial_state = build_initial_scheduler_state(
            kraken_state=KrakenState(),
            recorded_state=RecordedState(),
            report=ReconciliationReport(),
            now=NOW,
        )
        initial_state = replace(
            initial_state,
            bot_state=BotState(
                portfolio=Portfolio(cash_usd=Decimal("25")),
                beliefs=(
                    BeliefSnapshot(
                        pair="DOGE/USD",
                        direction=BeliefDirection.BEARISH,
                        confidence=0.9,
                        regime=MarketRegime.TRENDING,
                        sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
                    ),
                ),
            ),
            last_guardian_check_at=NOW,
            last_reconcile_at=NOW,
        )

        runtime = SchedulerRuntime(
            settings=_settings(ENABLE_CONDITIONAL_TREE="true"),
            executor=FakeExecutor(KrakenState()),
            conn=_memory_db(),
            initial_state=initial_state,
            scheduler_config=SchedulerConfig(
                cycle_interval_sec=1,
                reconcile_interval_sec=9999,
                guardian_interval_sec=9999,
            ),
            websocket_factory=websocket_factory,
            conditional_tree=conditional_tree,
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        await runtime.run_once()

        assert fake_websocket is not None
        assert conditional_tree.calls == 1
        assert fake_websocket.ticker_subscriptions == [("DOGE/USD",), ("BTC/USD",)]
        assert runtime.state.pending_belief_signals == (candidate_belief,)
        seeded = runtime.state.current_prices["BTC/USD"]
        assert isinstance(seeded, PriceSnapshot)
        assert seeded.price == Decimal("101.25")
        assert runtime._conditional_tree_state.is_active is True
        assert runtime._conditional_tree_state.chosen_candidate is not None
        assert runtime._conditional_tree_state.chosen_candidate.pair == "BTC/USD"

    asyncio.run(scenario())


def test_run_once_reaper_runs_before_scheduler() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        runtime._state = replace(
            runtime.state,
            last_reconcile_at=NOW,
            last_guardian_check_at=NOW,
        )
        call_order: list[str] = []

        async def remember(name: str) -> None:
            call_order.append(name)

        async def ensure_websocket_connected() -> None:
            await remember("websocket")

        async def ensure_subscriptions() -> None:
            await remember("subscriptions")

        async def reap_stale_orders(_now: datetime) -> None:
            await remember("reaper")

        class StubScheduler:
            def run_cycle(self, state):
                call_order.append("scheduler")
                return state, ()

        runtime._ensure_websocket_connected = AsyncMock(
            side_effect=ensure_websocket_connected
        )
        runtime._ensure_subscriptions = AsyncMock(side_effect=ensure_subscriptions)
        runtime._reap_stale_cc_orders = AsyncMock(side_effect=reap_stale_orders)
        runtime._maybe_bind_tree_to_position = AsyncMock(return_value=None)
        runtime._persist_state_changes = AsyncMock(return_value=None)
        runtime._maybe_poll_beliefs = AsyncMock(return_value=None)
        runtime._handle_effects = AsyncMock(return_value=None)
        runtime._maybe_plan_conditional_rotation = AsyncMock(return_value=None)
        runtime._maybe_run_rotation_planner = AsyncMock(return_value=None)
        runtime._scheduler = StubScheduler()

        await runtime.run_once()

        assert call_order[:4] == [
            "websocket",
            "subscriptions",
            "reaper",
            "scheduler",
        ]

    asyncio.run(scenario())


def test_run_once_recovers_from_missing_price() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        runtime._state = replace(
            runtime.state,
            last_reconcile_at=NOW,
            last_guardian_check_at=NOW,
        )
        recovered_effects = (LogEvent(message="recovered"),)

        class MissingPriceOnceScheduler:
            def __init__(self) -> None:
                self.calls = 0

            def run_cycle(self, state):
                self.calls += 1
                if self.calls == 1:
                    raise MissingCurrentPriceError("TRU/USD")
                seeded = state.current_prices.get("TRU/USD")
                assert isinstance(seeded, PriceSnapshot)
                assert seeded.price == Decimal("0.052")
                return state, recovered_effects

        scheduler = MissingPriceOnceScheduler()
        runtime._ensure_websocket_connected = AsyncMock(return_value=None)
        runtime._ensure_subscriptions = AsyncMock(return_value=None)
        runtime._reap_stale_cc_orders = AsyncMock(return_value=None)
        runtime._maybe_bind_tree_to_position = AsyncMock(return_value=None)
        runtime._persist_state_changes = AsyncMock(return_value=None)
        runtime._maybe_poll_beliefs = AsyncMock(return_value=None)
        runtime._handle_effects = AsyncMock(return_value=None)
        runtime._maybe_plan_conditional_rotation = AsyncMock(return_value=None)
        runtime._maybe_run_rotation_planner = AsyncMock(return_value=None)
        runtime._scheduler = scheduler

        with patch(
            "exchange.ohlcv.fetch_ohlcv",
            return_value=_bars_with_closes([0.052]),
        ) as fetch_ohlcv:
            effects = await runtime.run_once()

        assert effects == recovered_effects
        assert scheduler.calls == 2
        assert fetch_ohlcv.call_count == 1
        seeded = runtime.state.current_prices.get("TRU/USD")
        assert isinstance(seeded, PriceSnapshot)
        assert seeded.price == Decimal("0.052")
        assert runtime._last_runtime_error is None

    asyncio.run(scenario())


def test_apply_exit_offset_long_sell_goes_below_trigger() -> None:
    from runtime_loop import _apply_exit_offset

    price = Decimal("120")
    result = _apply_exit_offset(price, PositionSide.LONG, 0.1)
    assert result < price
    assert result == Decimal("119.8800")


def test_apply_exit_offset_short_buy_goes_above_trigger() -> None:
    from runtime_loop import _apply_exit_offset

    price = Decimal("0.18")
    result = _apply_exit_offset(price, PositionSide.SHORT, 0.1)
    assert result > price
    assert result == Decimal("0.1802")


def test_apply_exit_offset_zero_offset_returns_original() -> None:
    from runtime_loop import _apply_exit_offset

    price = Decimal("100")
    assert _apply_exit_offset(price, PositionSide.LONG, 0.0) == price


def test_apply_exit_offset_preserves_fine_precision() -> None:
    from runtime_loop import _apply_exit_offset

    price = Decimal("0.1234")
    result = _apply_exit_offset(price, PositionSide.LONG, 0.1)
    assert result == Decimal("0.1233")


def test_build_initial_scheduler_state_rehydrates_pending_exchange_order_id() -> None:
    pending = PendingOrder(
        client_order_id="cl-1",
        kind="rotation_entry",
        pair="DOGE/USD",
        side=OrderSide.BUY,
        base_qty=Decimal("100"),
        quote_qty=Decimal("12"),
    )

    state = build_initial_scheduler_state(
        kraken_state=KrakenState(),
        recorded_state=RecordedState(),
        report=ReconciliationReport(),
        now=NOW,
        persisted_pending_orders=((pending, "EX-1"),),
    )

    assert len(state.bot_state.pending_orders) == 1
    assert state.bot_state.pending_orders[0].exchange_order_id == "EX-1"


def test_execute_cancel_order_resolves_client_order_id_and_persists_cancelled_status() -> (
    None
):
    async def scenario() -> None:
        conn = _memory_db()
        writer = SqliteWriter(conn)
        reader = SqliteReader(conn)
        writer.upsert_order(
            "EX-1",
            "DOGE/USD",
            "cl-1",
            kind="rotation_entry",
            side="buy",
            exchange_order_id="EX-1",
        )
        executor = FakeExecutor(
            KrakenState(
                open_orders=(
                    KrakenOrder(
                        order_id="EX-1",
                        pair="DOGE/USD",
                        client_order_id="cl-1",
                        opened_at=NOW,
                    ),
                ),
            )
        )
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=executor,
            conn=conn,
            initial_state=build_initial_scheduler_state(
                kraken_state=executor.kraken_state,
                recorded_state=RecordedState(),
                report=ReconciliationReport(),
                now=NOW,
                persisted_pending_orders=(
                    (
                        PendingOrder(
                            client_order_id="cl-1",
                            kind="rotation_entry",
                            pair="DOGE/USD",
                            side=OrderSide.BUY,
                            base_qty=Decimal("100"),
                            quote_qty=Decimal("12"),
                        ),
                        "EX-1",
                    ),
                ),
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
                on_tick, on_fill
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        await runtime._execute_cancel_order(CancelOrder(client_order_id="cl-1"))

        assert executor.cancel_calls == ["EX-1"]
        assert reader.fetch_open_orders() == ()

    asyncio.run(scenario())


def test_reaper_writes_stale_order_cancelled_memory() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        _insert_open_order(
            conn,
            order_id="EX-STALE-1",
            created_at=NOW - timedelta(minutes=20),
        )
        executor = FakeExecutor(KrakenState())
        runtime = _runtime(
            settings=_settings(CC_ORDER_MAX_AGE_MINUTES="15"),
            conn=conn,
            executor=executor,
        )

        await runtime._reap_stale_cc_orders(NOW)

        assert executor.cancel_calls == ["EX-STALE-1"]
        row = conn.execute(
            "SELECT status FROM orders WHERE order_id = ?",
            ("EX-STALE-1",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "cancelled"

        memory_row = conn.execute(
            "SELECT timestamp, category, pair, content, importance FROM cc_memory ORDER BY id"
        ).fetchone()
        assert memory_row is not None
        assert memory_row["timestamp"] == NOW.isoformat()
        assert memory_row["category"] == "stale_order_cancelled"
        assert memory_row["pair"] == "PEPE/USD"
        assert memory_row["importance"] == 0.6
        assert json.loads(memory_row["content"]) == {
            "order_id": "EX-STALE-1",
            "age_minutes": 20,
            "limit_price": "0.000003512",
            "side": "sell",
            "base_qty": "10348231.25",
        }

    asyncio.run(scenario())


def test_reaper_skips_fresh_cc_order() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        _insert_open_order(
            conn,
            order_id="EX-FRESH-1",
            created_at=NOW - timedelta(minutes=5),
        )
        executor = FakeExecutor(KrakenState())
        runtime = _runtime(
            settings=_settings(CC_ORDER_MAX_AGE_MINUTES="15"),
            conn=conn,
            executor=executor,
        )

        await runtime._reap_stale_cc_orders(NOW)

        assert executor.cancel_calls == []
        row = conn.execute(
            "SELECT status FROM orders WHERE order_id = ?",
            ("EX-FRESH-1",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "open"
        memory_count = conn.execute(
            "SELECT COUNT(*) AS count FROM cc_memory WHERE category = ?",
            ("stale_order_cancelled",),
        ).fetchone()
        assert memory_count is not None
        assert memory_count["count"] == 0

    asyncio.run(scenario())


def test_reaper_skips_rotation_entry_orders() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        _insert_open_order(
            conn,
            order_id="EX-ROT-1",
            created_at=NOW - timedelta(minutes=20),
            kind="rotation_entry",
            pair="DOGE/USD",
            side="buy",
            base_qty=Decimal("125"),
            limit_price=Decimal("0.1234"),
        )
        executor = FakeExecutor(KrakenState())
        runtime = _runtime(
            settings=_settings(CC_ORDER_MAX_AGE_MINUTES="15"),
            conn=conn,
            executor=executor,
        )

        await runtime._reap_stale_cc_orders(NOW)

        assert executor.cancel_calls == []
        row = conn.execute(
            "SELECT status FROM orders WHERE order_id = ?",
            ("EX-ROT-1",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "open"

    asyncio.run(scenario())


def test_reaper_handles_already_filled_order() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        _insert_open_order(
            conn,
            order_id="EX-GONE-1",
            created_at=NOW - timedelta(minutes=20),
        )

        class NotFoundCancelExecutor(FakeExecutor):
            def execute_cancel(self, order_id: str) -> int:
                self.cancel_calls.append(order_id)
                raise CancelOrderNotFoundError(order_id)

        executor = NotFoundCancelExecutor(KrakenState())
        runtime = _runtime(
            settings=_settings(CC_ORDER_MAX_AGE_MINUTES="15"),
            conn=conn,
            executor=executor,
        )

        await runtime._reap_stale_cc_orders(NOW)

        assert executor.cancel_calls == ["EX-GONE-1"]
        row = conn.execute(
            "SELECT status FROM orders WHERE order_id = ?",
            ("EX-GONE-1",),
        ).fetchone()
        assert row is not None
        assert row["status"] == "cancelled"

    asyncio.run(scenario())


def test_reaper_writes_stale_order_memory() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        _insert_open_order(
            conn,
            order_id="EX-MEM-1",
            created_at=NOW - timedelta(minutes=20),
        )
        _insert_open_order(
            conn,
            order_id="EX-MEM-2",
            created_at=NOW - timedelta(minutes=25),
            pair="DOGE/USD",
            side="buy",
            base_qty=Decimal("125"),
            limit_price=Decimal("0.1234"),
        )
        executor = FakeExecutor(KrakenState())
        runtime = _runtime(
            settings=_settings(CC_ORDER_MAX_AGE_MINUTES="15"),
            conn=conn,
            executor=executor,
        )

        await runtime._reap_stale_cc_orders(NOW)

        rows = conn.execute(
            "SELECT category, pair, content FROM cc_memory ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert [row["category"] for row in rows] == [
            "stale_order_cancelled",
            "stale_order_cancelled",
        ]
        assert [row["pair"] for row in rows] == ["PEPE/USD", "DOGE/USD"]

    asyncio.run(scenario())


def test_recon_discrepancy_persists_to_cc_memory() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        current_time = NOW
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(KrakenState()),
            conn=conn,
            initial_state=build_initial_scheduler_state(
                kraken_state=KrakenState(),
                recorded_state=RecordedState(),
                report=ReconciliationReport(),
                now=NOW,
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
                on_tick, on_fill
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: current_time,
        )
        report = _reconciliation_report_with_untracked_assets(
            "FLOW", "HYPE", "MON", "TRIA"
        )

        await runtime._handle_effects(
            (
                ReconciliationDiscrepancy(
                    report=report,
                    summary="untracked_assets=4",
                ),
            )
        )

        rows = conn.execute(
            "SELECT category, pair, content, importance FROM cc_memory ORDER BY id"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["category"] == "reconciliation_anomaly"
        assert rows[0]["pair"] is None
        assert rows[0]["importance"] == 0.7
        assert json.loads(rows[0]["content"]) == {
            "ghost_positions": 0,
            "foreign_orders": 0,
            "fee_drift": 0,
            "untracked_assets": 4,
            "untracked_asset_symbols": ["FLOW", "HYPE", "MON", "TRIA"],
        }

    asyncio.run(scenario())


def test_recon_discrepancy_dedupe_within_5min() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        current_time = NOW
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(KrakenState()),
            conn=conn,
            initial_state=build_initial_scheduler_state(
                kraken_state=KrakenState(),
                recorded_state=RecordedState(),
                report=ReconciliationReport(),
                now=NOW,
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
                on_tick, on_fill
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: current_time,
        )
        discrepancy = ReconciliationDiscrepancy(
            report=_reconciliation_report_with_untracked_assets(
                "FLOW", "HYPE", "MON", "TRIA"
            ),
            summary="untracked_assets=4",
        )

        await runtime._handle_effects((discrepancy,))
        await runtime._handle_effects((discrepancy,))

        row = conn.execute(
            "SELECT COUNT(*) AS count FROM cc_memory WHERE category = ?",
            ("reconciliation_anomaly",),
        ).fetchone()
        assert row is not None
        assert row["count"] == 1

    asyncio.run(scenario())


def test_recon_discrepancy_writes_again_after_dedupe_window() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        current_time = NOW
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(KrakenState()),
            conn=conn,
            initial_state=build_initial_scheduler_state(
                kraken_state=KrakenState(),
                recorded_state=RecordedState(),
                report=ReconciliationReport(),
                now=NOW,
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
                on_tick, on_fill
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: current_time,
        )
        discrepancy = ReconciliationDiscrepancy(
            report=_reconciliation_report_with_untracked_assets(
                "FLOW", "HYPE", "MON", "TRIA"
            ),
            summary="untracked_assets=4",
        )

        await runtime._handle_effects((discrepancy,))
        current_time = NOW + timedelta(minutes=6)
        await runtime._handle_effects((discrepancy,))

        row = conn.execute(
            "SELECT COUNT(*) AS count FROM cc_memory WHERE category = ?",
            ("reconciliation_anomaly",),
        ).fetchone()
        assert row is not None
        assert row["count"] == 2

    asyncio.run(scenario())


def test_reconcile_pending_orders_recovers_fill_from_trade_history() -> None:
    async def scenario() -> None:
        conn = _memory_db()
        writer = SqliteWriter(conn)
        reader = SqliteReader(conn)
        writer.upsert_order(
            "EX-2",
            "DOGE/USD",
            "cl-2",
            kind="position_entry",
            side="buy",
            base_qty=Decimal("125"),
            quote_qty=Decimal("15.425"),
            exchange_order_id="EX-2",
        )
        kraken_state = KrakenState(
            trade_history=(
                KrakenTrade(
                    trade_id="T-1",
                    pair="DOGE/USD",
                    order_id="EX-2",
                    client_order_id="cl-2",
                    side="buy",
                    quantity=Decimal("125"),
                    price=Decimal("0.1234"),
                    fee=Decimal("0.05"),
                    filled_at=NOW,
                ),
            ),
        )
        executor = FakeExecutor(kraken_state)
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=executor,
            conn=conn,
            initial_state=build_initial_scheduler_state(
                kraken_state=kraken_state,
                recorded_state=RecordedState(),
                report=ReconciliationReport(),
                now=NOW,
                persisted_pending_orders=(
                    (
                        PendingOrder(
                            client_order_id="cl-2",
                            kind="position_entry",
                            pair="DOGE/USD",
                            side=OrderSide.BUY,
                            base_qty=Decimal("125"),
                            quote_qty=Decimal("15.425"),
                            position_id="pos-2",
                        ),
                        "EX-2",
                    ),
                ),
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
                on_tick, on_fill
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        await runtime._reconcile_pending_orders(
            now=NOW, kraken_state=kraken_state, source="startup"
        )

        assert runtime.state.bot_state.pending_orders == ()
        assert len(runtime.state.bot_state.portfolio.positions) == 1
        assert runtime.state.bot_state.portfolio.positions[0].position_id == "pos-2"
        assert reader.fetch_open_orders() == ()
        ledger_rows = conn.execute(
            "SELECT pair, quantity, price, fee FROM ledger ORDER BY id"
        ).fetchall()
        assert len(ledger_rows) == 1
        assert ledger_rows[0]["pair"] == "DOGE/USD"
        assert ledger_rows[0]["quantity"] == "125"
        assert ledger_rows[0]["price"] == "0.1234"
        assert ledger_rows[0]["fee"] == "0.05"

    asyncio.run(scenario())


def test_rotation_tree_rehydrates_persisted_child_nodes() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    root = RotationNode(
        node_id="root-usd",
        parent_node_id=None,
        depth=0,
        asset="USD",
        quantity_total=Decimal("100"),
        quantity_free=Decimal("70"),
        quantity_reserved=Decimal("30"),
        status=RotationNodeStatus.OPEN,
    )
    child = RotationNode(
        node_id="root-usd-eth-0",
        parent_node_id="root-usd",
        depth=1,
        asset="ETH",
        quantity_total=Decimal("0.01"),
        quantity_free=Decimal("0.01"),
        entry_pair="ETH/USD",
        from_asset="USD",
        order_side=OrderSide.BUY,
        entry_price=Decimal("3000"),
        fill_price=Decimal("3000"),
        entry_cost=Decimal("30"),
        take_profit_price=Decimal("3090"),
        stop_loss_price=Decimal("2940"),
        trailing_stop_high=Decimal("3000"),
        status=RotationNodeStatus.OPEN,
        opened_at=NOW,
    )
    writer.save_rotation_tree(
        RotationTreeState(
            nodes=(root, child),
            root_node_ids=("root-usd",),
        )
    )
    runtime = SchedulerRuntime(
        settings=_settings(ENABLE_ROTATION_TREE="true"),
        executor=FakeExecutor(
            KrakenState(
                balances=(
                    Balance(asset="USD", available=Decimal("100"), held=Decimal("0")),
                ),
            )
        ),
        conn=conn,
        initial_state=build_initial_scheduler_state(
            kraken_state=KrakenState(
                balances=(
                    Balance(asset="USD", available=Decimal("100"), held=Decimal("0")),
                ),
            ),
            recorded_state=RecordedState(),
            report=ReconciliationReport(),
            now=NOW,
        ),
        websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(
            on_tick, on_fill
        ),
        serve_dashboard=False,
        sse_publisher=_noop_publish,
        heartbeat_writer=lambda snapshot: None,
        utc_now=lambda: NOW,
    )

    assert runtime._rotation_tree is not None
    assert {node.node_id for node in runtime._rotation_tree.nodes} == {
        "root-usd",
        "root-usd-eth-0",
    }


def test_rotation_entry_fill_sets_fee_aware_buy_tp_and_sl() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-usd",
            parent_node_id=None,
            depth=0,
            asset="USD",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("0"),
            quantity_reserved=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        child = RotationNode(
            node_id="root-usd-eth-0",
            parent_node_id="root-usd",
            depth=1,
            asset="ETH",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("100"),
            entry_pair="ETH/USD",
            from_asset="USD",
            order_side=OrderSide.BUY,
            entry_price=Decimal("100"),
            status=RotationNodeStatus.PLANNED,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child),
            root_node_ids=("root-usd",),
        )
        runtime._rotation_fill_queue.append(
            (child.node_id, Decimal("1"), Decimal("100"), "rotation_entry", None)
        )

        await runtime._settle_rotation_fills(NOW)

        opened = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == child.node_id
        )
        assert opened.take_profit_price == Decimal("105.52")
        assert opened.stop_loss_price == Decimal("97.9")

        tp_net_pct = (
            (opened.take_profit_price / opened.fill_price - Decimal("1"))
            * Decimal("100")
        ) - Decimal(str(runtime._settings.kraken_maker_fee_pct * 2))
        sl_net_loss_pct = (
            (Decimal("1") - opened.stop_loss_price / opened.fill_price) * Decimal("100")
        ) + Decimal(str(runtime._settings.kraken_taker_fee_pct))

        assert tp_net_pct == Decimal(str(runtime._settings.rotation_take_profit_pct))
        assert sl_net_loss_pct == Decimal(str(runtime._settings.rotation_stop_loss_pct))

    asyncio.run(scenario())


def test_rotation_entry_fill_sets_fee_aware_sell_tp_and_sl() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-doge",
            parent_node_id=None,
            depth=0,
            asset="DOGE",
            quantity_total=Decimal("1"),
            quantity_free=Decimal("0"),
            quantity_reserved=Decimal("1"),
            status=RotationNodeStatus.OPEN,
        )
        child = RotationNode(
            node_id="root-doge-usd-0",
            parent_node_id="root-doge",
            depth=1,
            asset="USD",
            quantity_total=Decimal("1"),
            quantity_free=Decimal("1"),
            entry_pair="DOGE/USD",
            from_asset="DOGE",
            order_side=OrderSide.SELL,
            entry_price=Decimal("100"),
            status=RotationNodeStatus.PLANNED,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child),
            root_node_ids=("root-doge",),
        )
        runtime._rotation_fill_queue.append(
            (child.node_id, Decimal("1"), Decimal("100"), "rotation_entry", None)
        )

        await runtime._settle_rotation_fills(NOW)

        opened = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == child.node_id
        )
        assert opened.take_profit_price == Decimal("94.48")
        assert opened.stop_loss_price == Decimal("102.1")

        tp_net_pct = (
            (Decimal("1") - opened.take_profit_price / opened.fill_price)
            * Decimal("100")
        ) - Decimal(str(runtime._settings.kraken_maker_fee_pct * 2))
        sl_net_loss_pct = (
            (opened.stop_loss_price / opened.fill_price - Decimal("1")) * Decimal("100")
        ) + Decimal(str(runtime._settings.kraken_taker_fee_pct))

        assert tp_net_pct == Decimal(str(runtime._settings.rotation_take_profit_pct))
        assert sl_net_loss_pct == Decimal(str(runtime._settings.rotation_stop_loss_pct))

    asyncio.run(scenario())


def test_rotation_exit_fill_persists_trade_outcome() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-usd",
            parent_node_id=None,
            depth=0,
            asset="USD",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("0"),
            status=RotationNodeStatus.OPEN,
        )
        child = RotationNode(
            node_id="root-usd-eth-0",
            parent_node_id="root-usd",
            depth=1,
            asset="ETH",
            quantity_total=Decimal("1"),
            quantity_free=Decimal("0"),
            entry_pair="ETH/USD",
            from_asset="USD",
            order_side=OrderSide.BUY,
            entry_price=Decimal("100"),
            fill_price=Decimal("100"),
            entry_cost=Decimal("100"),
            opened_at=NOW - timedelta(hours=2),
            confidence=0.83,
            exit_reason="take_profit",
            status=RotationNodeStatus.CLOSING,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child),
            root_node_ids=("root-usd",),
        )
        runtime._rotation_fill_queue.append(
            (
                child.node_id,
                Decimal("1"),
                Decimal("110"),
                "rotation_exit",
                Decimal("0.25"),
            )
        )

        await runtime._settle_rotation_fills(NOW)

        outcomes = runtime._reader.fetch_trade_outcomes(lookback_days=3650)
        assert len(outcomes) == 1
        outcome = outcomes[0]
        assert outcome["node_id"] == child.node_id
        assert outcome["pair"] == "ETH/USD"
        assert outcome["direction"] == "buy"
        assert outcome["entry_price"] == "100"
        assert outcome["exit_price"] == "110"
        assert outcome["entry_cost"] == "100"
        assert outcome["exit_proceeds"] == "110"
        assert outcome["net_pnl"] == "10"
        assert outcome["fee_total"] == "0.25"
        assert outcome["exit_reason"] == "take_profit"
        assert outcome["confidence"] == 0.83
        assert outcome["hold_hours"] == 2.0
        assert outcome["node_depth"] == 1

    asyncio.run(scenario())


def test_root_rotation_exit_fill_persists_trade_outcome_depth_zero() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-ada",
            parent_node_id=None,
            depth=0,
            asset="ADA",
            quantity_total=Decimal("10"),
            quantity_free=Decimal("0"),
            entry_pair="ADA/USD",
            order_side=OrderSide.BUY,
            entry_price=Decimal("2"),
            fill_price=Decimal("2"),
            entry_cost=Decimal("20"),
            opened_at=NOW - timedelta(hours=3),
            confidence=0.67,
            exit_reason="root_exit_bearish",
            status=RotationNodeStatus.CLOSING,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._rotation_fill_queue.append(
            (
                root.node_id,
                Decimal("10"),
                Decimal("2.1"),
                "rotation_exit",
                Decimal("0.05"),
            )
        )

        await runtime._settle_rotation_fills(NOW)

        outcomes = runtime._reader.fetch_trade_outcomes(lookback_days=3650)
        assert len(outcomes) == 1
        assert outcomes[0]["node_depth"] == 0

    asyncio.run(scenario())


def test_cc_brain_mode_skips_planner() -> None:
    async def scenario() -> None:
        runtime = _runtime(settings=_settings(CC_BRAIN_MODE="true"))
        runtime._rotation_planner = object()
        runtime._rotation_tree = RotationTreeState(nodes=(), root_node_ids=())

        async def fail_handle_rotation_expiry(_now: datetime) -> None:
            raise AssertionError("planner preflight should be skipped in CC brain mode")

        runtime._handle_rotation_expiry = fail_handle_rotation_expiry  # type: ignore[method-assign]

        await runtime._maybe_run_rotation_planner(NOW)

    asyncio.run(scenario())


def test_cc_brain_mode_skips_root_eval() -> None:
    async def scenario() -> None:
        runtime = _runtime(settings=_settings(CC_BRAIN_MODE="true"))
        root = RotationNode(
            node_id="root-ada",
            parent_node_id=None,
            depth=0,
            asset="ADA",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = object()

        def fail_find_root_exit_pair(_asset: str) -> tuple[str, OrderSide] | None:
            raise AssertionError("root evaluation should be skipped in CC brain mode")

        runtime._find_root_exit_pair = fail_find_root_exit_pair  # type: ignore[method-assign]

        await runtime._evaluate_root_deadlines(NOW)

    asyncio.run(scenario())


def test_evaluate_root_deadlines_sets_opened_at_once() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-ada",
            parent_node_id=None,
            depth=0,
            asset="ADA",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = FakeRootPairScanner(
            _bars_with_closes([0.5] * 50),
            pairs=(("ADA/USD", "ADA", "USD"),),
        )
        runtime._root_usd_prices = {"USD": Decimal("1"), "ADA": Decimal("0.5")}
        runtime._root_usd_prices_at = 10**12

        with patch("runtime_loop.evaluate_root_ta", return_value=("bullish", 12.0, 0.9)):
            await runtime._evaluate_root_deadlines(NOW)

        updated = next(n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id)
        assert updated.opened_at == NOW
        assert updated.deadline_at == NOW + timedelta(hours=12)

        first_opened_at = updated.opened_at
        first_deadline = updated.deadline_at

        with patch("runtime_loop.evaluate_root_ta", return_value=("bearish", 6.0, 0.2)):
            await runtime._evaluate_root_deadlines(NOW + timedelta(hours=1))

        updated_again = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id
        )
        assert updated_again.opened_at == first_opened_at
        assert updated_again.deadline_at == first_deadline

    asyncio.run(scenario())


def test_recovery_exhausted_calls_close() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-akt",
            parent_node_id=None,
            depth=0,
            asset="AKT",
            quantity_total=Decimal("10"),
            quantity_free=Decimal("10"),
            entry_pair="AKT/USD",
            order_side=OrderSide.BUY,
            status=RotationNodeStatus.EXPIRED,
            recovery_count=3,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = FakeRootPairScanner(
            _bars_with_closes([1.0] * 50),
            pairs=(("AKT/USD", "AKT", "USD"),),
        )
        runtime._root_usd_prices_at = 10**12

        close_calls: list[dict[str, object]] = []

        async def fake_close(
            node, *, reason: str = "", now: datetime | None = None, order_type=None
        ) -> None:
            close_calls.append(
                {"node_id": node.node_id, "reason": reason, "now": now}
            )

        runtime._close_rotation_node = fake_close  # type: ignore[method-assign]

        await runtime._evaluate_root_deadlines(NOW)

        assert close_calls == [
            {
                "node_id": root.node_id,
                "reason": RotationExitReason.RECOVERY_EXHAUSTED.value,
                "now": NOW,
            }
        ]
        updated = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id
        )
        assert updated.exit_reason == RotationExitReason.RECOVERY_EXHAUSTED.value

    asyncio.run(scenario())


def test_recovery_under_limit_resets_to_open() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-akt",
            parent_node_id=None,
            depth=0,
            asset="AKT",
            quantity_total=Decimal("10"),
            quantity_free=Decimal("10"),
            status=RotationNodeStatus.EXPIRED,
            deadline_at=NOW - timedelta(hours=1),
            recovery_count=2,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = FakeRootPairScanner(
            _bars_with_closes([1.0] * 50),
            pairs=(("AKT/USD", "AKT", "USD"),),
        )
        runtime._root_usd_prices_at = 10**12

        close_calls: list[str] = []

        async def fake_close(
            node, *, reason: str = "", now: datetime | None = None, order_type=None
        ) -> None:
            del node, reason, now, order_type
            close_calls.append("called")

        runtime._close_rotation_node = fake_close  # type: ignore[method-assign]

        await runtime._evaluate_root_deadlines(NOW)

        updated = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id
        )
        assert updated.status == RotationNodeStatus.OPEN
        assert updated.deadline_at is None
        assert updated.recovery_count == 3
        assert close_calls == []

    asyncio.run(scenario())


def test_recovery_exhausted_no_entry_pair() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-akt",
            parent_node_id=None,
            depth=0,
            asset="AKT",
            quantity_total=Decimal("10"),
            quantity_free=Decimal("10"),
            order_side=OrderSide.BUY,
            status=RotationNodeStatus.EXPIRED,
            recovery_count=3,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = FakeRootPairScanner(
            _bars_with_closes([1.0] * 50),
            pairs=(("AKT/USD", "AKT", "USD"),),
        )
        runtime._root_usd_prices_at = 10**12

        close_calls: list[dict[str, object]] = []
        real_close = runtime._close_rotation_node

        async def spy_close(
            node, *, reason: str = "", now: datetime | None = None, order_type=None
        ) -> None:
            close_calls.append(
                {"node_id": node.node_id, "reason": reason, "now": now}
            )
            await real_close(node, reason=reason, now=now, order_type=order_type)

        runtime._close_rotation_node = spy_close  # type: ignore[method-assign]

        await runtime._evaluate_root_deadlines(NOW)

        assert close_calls == [
            {
                "node_id": root.node_id,
                "reason": RotationExitReason.RECOVERY_EXHAUSTED.value,
                "now": NOW,
            }
        ]
        updated = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id
        )
        assert updated.status == RotationNodeStatus.EXPIRED
        assert updated.exit_reason == RotationExitReason.RECOVERY_EXHAUSTED.value

    asyncio.run(scenario())


def test_handle_root_expiry_recalculates_entry_cost_from_last_close() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-ada",
            parent_node_id=None,
            depth=0,
            asset="ADA",
            quantity_total=Decimal("25"),
            quantity_free=Decimal("25"),
            entry_cost=Decimal("999"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = FakeRootPairScanner(
            _bars_with_closes(([0.4] * 49) + [0.42]),
            pairs=(("ADA/USD", "ADA", "USD"),),
        )

        closed_nodes: list[RotationNode] = []

        async def fake_close(node, *, reason: str, now: datetime, order_type=None) -> None:
            closed_nodes.append(node)

        runtime._close_rotation_node = fake_close  # type: ignore[method-assign]

        with patch("runtime_loop.evaluate_root_ta", return_value=("bearish", 8.0, 0.7)):
            await runtime._handle_root_expiry(root, NOW)

        updated = next(n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id)
        expected_entry_cost = Decimal("25") * Decimal("0.42")
        assert updated.entry_price == Decimal("0.42")
        assert updated.entry_cost == expected_entry_cost
        assert updated.exit_reason == "root_exit_bearish"
        assert closed_nodes
        assert closed_nodes[0].entry_cost == expected_entry_cost

    asyncio.run(scenario())


def test_handle_root_expiry_keeps_quote_side_root_entry_cost_in_quote_units() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-usd",
            parent_node_id=None,
            depth=0,
            asset="USD",
            quantity_total=Decimal("36.9612"),
            quantity_free=Decimal("36.9612"),
            entry_cost=Decimal("999"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._pair_scanner = FakeRootPairScanner(
            _bars_with_closes(([1.0] * 49) + [0.99965]),
            pairs=(("USDT/USD", "USDT", "USD"),),
        )

        closed_nodes: list[RotationNode] = []

        async def fake_close(node, *, reason: str, now: datetime, order_type=None) -> None:
            closed_nodes.append(node)

        runtime._close_rotation_node = fake_close  # type: ignore[method-assign]

        with patch("runtime_loop.evaluate_root_ta", return_value=("bearish", 8.0, 0.7)):
            await runtime._handle_root_expiry(root, NOW)

        updated = next(n for n in runtime._rotation_tree.nodes if n.node_id == root.node_id)
        assert updated.entry_price == Decimal("0.99965")
        assert updated.entry_cost == Decimal("36.9612")
        assert updated.order_side == OrderSide.SELL
        assert updated.exit_reason == "root_exit_bearish"
        assert closed_nodes
        assert closed_nodes[0].entry_cost == Decimal("36.9612")

    asyncio.run(scenario())


def test_monitor_rotation_prices_ratchets_buy_stop_after_trailing_activation() -> None:
    async def scenario() -> None:
        runtime = _runtime()
        root = RotationNode(
            node_id="root-usd",
            parent_node_id=None,
            depth=0,
            asset="USD",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        child = RotationNode(
            node_id="root-usd-eth-0",
            parent_node_id="root-usd",
            depth=1,
            asset="ETH",
            quantity_total=Decimal("1"),
            quantity_free=Decimal("1"),
            entry_pair="ETH/USD",
            from_asset="USD",
            order_side=OrderSide.BUY,
            entry_price=Decimal("100"),
            fill_price=Decimal("100"),
            entry_cost=Decimal("100"),
            take_profit_price=Decimal("110"),
            stop_loss_price=Decimal("97.5"),
            trailing_stop_high=Decimal("100"),
            status=RotationNodeStatus.OPEN,
            opened_at=NOW,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child),
            root_node_ids=("root-usd",),
        )
        runtime._state = replace(
            runtime._state,
            current_prices={"ETH/USD": PriceSnapshot(price=Decimal("105"))},
        )

        await runtime._monitor_rotation_prices(NOW)

        updated = next(
            n for n in runtime._rotation_tree.nodes if n.node_id == child.node_id
        )
        assert updated.trailing_stop_high == Decimal("105")
        assert updated.stop_loss_price == Decimal("102.375")

    asyncio.run(scenario())


def test_monitor_rotation_prices_triggers_root_stop_loss_and_skips_quote_roots() -> (
    None
):
    async def scenario() -> None:
        runtime = _runtime()
        risk_root = RotationNode(
            node_id="root-ada",
            parent_node_id=None,
            depth=0,
            asset="ADA",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("100"),
            entry_pair="ADA/USD",
            order_side=OrderSide.BUY,
            entry_cost=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        stable_root = RotationNode(
            node_id="root-usdt",
            parent_node_id=None,
            depth=0,
            asset="USDT",
            quantity_total=Decimal("100"),
            quantity_free=Decimal("100"),
            entry_pair="USDT/USD",
            order_side=OrderSide.BUY,
            entry_cost=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(risk_root, stable_root),
            root_node_ids=("root-ada", "root-usdt"),
        )
        runtime._root_usd_prices = {
            "USD": Decimal("1"),
            "ADA": Decimal("0.89"),
            "USDT": Decimal("0.89"),
        }
        close_calls: list[tuple[str, OrderType]] = []

        async def close_stub(
            node, *, reason: str = "", now=None, order_type=OrderType.LIMIT
        ):
            del reason, now
            close_calls.append((node.node_id, order_type))

        runtime._close_rotation_node = close_stub  # type: ignore[method-assign]

        await runtime._monitor_rotation_prices(NOW)

        nodes = {node.node_id: node for node in runtime._rotation_tree.nodes}
        assert close_calls == [("root-ada", OrderType.MARKET)]
        assert nodes["root-ada"].exit_reason == "root_stop_loss"
        assert nodes["root-usdt"].exit_reason is None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Phase 8B: 15M momentum confirmation gate tests
# ---------------------------------------------------------------------------


class FakeTechnicalSource15M:
    """Fake technical source that returns a fixed belief for any pair/bars."""

    def __init__(self, direction: BeliefDirection, confidence: float = 0.67) -> None:
        self.direction = direction
        self.confidence = confidence
        self.min_bars = 10  # Low threshold so test bars pass

    def analyze(self, pair: str, bars) -> BeliefSnapshot:
        return BeliefSnapshot(
            pair=pair,
            direction=self.direction,
            confidence=self.confidence,
            regime=MarketRegime.TRENDING,
            sources=(),
        )


class FakePairScannerWith15M:
    """Minimal pair scanner with _technical_source for 15M gate."""

    def __init__(self, direction: BeliefDirection) -> None:
        self._technical_source = FakeTechnicalSource15M(direction)

    def discover_asset_pairs(self, _asset):
        return ()

    def _ohlcv_fetcher(self, _pair, **_kw):
        return _bars_with_closes([1.0] * 50)


def _planned_child(node_id: str = "child-0", parent_id: str = "root-usd") -> RotationNode:
    return RotationNode(
        node_id=node_id,
        parent_node_id=parent_id,
        depth=1,
        asset="ETH",
        quantity_total=Decimal("1"),
        quantity_free=Decimal("1"),
        status=RotationNodeStatus.PLANNED,
        entry_pair="ETH/USD",
        from_asset="USD",
        order_side=OrderSide.BUY,
        entry_price=Decimal("20"),
        confidence=0.8,
    )


def test_15m_opposing_defers_planned() -> None:
    """15M bearish on BUY node → node stays PLANNED (deferred)."""

    async def scenario() -> None:
        runtime = _runtime(settings=_settings(MTF_15M_CONFIRM_ENABLED="true"))
        child = _planned_child()
        root = RotationNode(
            node_id="root-usd", parent_node_id=None, depth=0, asset="USD",
            quantity_total=Decimal("100"), quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child), root_node_ids=("root-usd",),
        )
        runtime._pair_scanner = FakePairScannerWith15M(BeliefDirection.BEARISH)

        with patch("exchange.ohlcv.fetch_ohlcv", return_value=_bars_with_closes([1.0] * 50)):
            await runtime._execute_rotation_entries(NOW)

        node = next(n for n in runtime._rotation_tree.nodes if n.node_id == child.node_id)
        assert node.status == RotationNodeStatus.PLANNED
        assert runtime._mtf_15m_deferral_counts.get(child.node_id) == 1

    asyncio.run(scenario())


def test_15m_aligned_proceeds() -> None:
    """15M bullish on BUY node → deferral count reset, execution proceeds."""

    async def scenario() -> None:
        runtime = _runtime(settings=_settings(MTF_15M_CONFIRM_ENABLED="true"))
        child = _planned_child()
        root = RotationNode(
            node_id="root-usd", parent_node_id=None, depth=0, asset="USD",
            quantity_total=Decimal("100"), quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child), root_node_ids=("root-usd",),
        )
        runtime._pair_scanner = FakePairScannerWith15M(BeliefDirection.BULLISH)
        runtime._mtf_15m_deferral_counts["child-0"] = 3  # pre-set

        with patch("exchange.ohlcv.fetch_ohlcv", return_value=_bars_with_closes([1.0] * 50)):
            await runtime._execute_rotation_entries(NOW)

        # Deferral count should be cleared on passthrough
        assert "child-0" not in runtime._mtf_15m_deferral_counts

    asyncio.run(scenario())


def test_15m_max_deferrals_cancels() -> None:
    """6 consecutive 15M rejections → node cancelled."""

    async def scenario() -> None:
        runtime = _runtime(settings=_settings(
            MTF_15M_CONFIRM_ENABLED="true", MTF_15M_MAX_DEFERRALS="6",
        ))
        child = _planned_child()
        root = RotationNode(
            node_id="root-usd", parent_node_id=None, depth=0, asset="USD",
            quantity_total=Decimal("100"), quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child), root_node_ids=("root-usd",),
        )
        runtime._pair_scanner = FakePairScannerWith15M(BeliefDirection.BEARISH)
        runtime._mtf_15m_deferral_counts[child.node_id] = 5  # one more → cancel

        with patch("exchange.ohlcv.fetch_ohlcv", return_value=_bars_with_closes([1.0] * 50)):
            await runtime._execute_rotation_entries(NOW)

        node = next(n for n in runtime._rotation_tree.nodes if n.node_id == child.node_id)
        assert node.status == RotationNodeStatus.CANCELLED
        assert child.node_id not in runtime._mtf_15m_deferral_counts

    asyncio.run(scenario())


def test_15m_gate_disabled() -> None:
    """MTF_15M_CONFIRM_ENABLED=False → no 15M check, proceeds directly."""

    async def scenario() -> None:
        runtime = _runtime(settings=_settings(MTF_15M_CONFIRM_ENABLED="false"))
        child = _planned_child()
        root = RotationNode(
            node_id="root-usd", parent_node_id=None, depth=0, asset="USD",
            quantity_total=Decimal("100"), quantity_free=Decimal("100"),
            status=RotationNodeStatus.OPEN,
        )
        runtime._rotation_tree = RotationTreeState(
            nodes=(root, child), root_node_ids=("root-usd",),
        )
        # Pair scanner with BEARISH 15M — would block if enabled
        runtime._pair_scanner = FakePairScannerWith15M(BeliefDirection.BEARISH)

        # Should NOT defer — gate is disabled
        await runtime._execute_rotation_entries(NOW)

        assert child.node_id not in runtime._mtf_15m_deferral_counts

    asyncio.run(scenario())


def test_tree_value_excludes_orphan_roots() -> None:
    runtime = _runtime()
    live_root = RotationNode(
        node_id="root-ada",
        parent_node_id=None,
        depth=0,
        asset="ADA",
        quantity_total=Decimal("100"),
        quantity_free=Decimal("100"),
        status=RotationNodeStatus.OPEN,
    )
    orphan_root = RotationNode(
        node_id="root-sol",
        parent_node_id=None,
        depth=0,
        asset="SOL",
        quantity_total=Decimal("50"),
        quantity_free=Decimal("50"),
        status=RotationNodeStatus.OPEN,
    )
    runtime._rotation_tree = RotationTreeState(
        nodes=(live_root, orphan_root),
        root_node_ids=(live_root.node_id, orphan_root.node_id),
    )
    runtime._root_usd_prices = {
        "USD": Decimal("1"),
        "ADA": Decimal("1"),
        "SOL": Decimal("2"),
    }
    runtime._state = replace(
        runtime.state,
        bot_state=replace(
            runtime.state.bot_state,
            balances=(Balance(asset="ADA", available=Decimal("100")),),
            portfolio=Portfolio(total_value_usd=Decimal("100")),
        ),
        current_prices={
            "ADA/USD": PriceSnapshot(price=Decimal("1")),
            "SOL/USD": PriceSnapshot(price=Decimal("2")),
        },
    )

    dashboard = runtime._build_dashboard_state(runtime.state)

    nodes = {node.node_id: node for node in runtime._rotation_tree.nodes}
    assert dashboard.rotation_tree.total_portfolio_value_usd == "100.00"
    assert nodes["root-ada"].status == RotationNodeStatus.OPEN
    assert nodes["root-sol"].status == RotationNodeStatus.CLOSED


def test_orphan_root_prune_writes_memory() -> None:
    conn = _memory_db()
    runtime = _runtime(conn=conn)
    orphan_root = RotationNode(
        node_id="root-sol",
        parent_node_id=None,
        depth=0,
        asset="SOL",
        quantity_total=Decimal("50"),
        quantity_free=Decimal("50"),
        status=RotationNodeStatus.OPEN,
    )
    runtime._rotation_tree = RotationTreeState(
        nodes=(orphan_root,),
        root_node_ids=(orphan_root.node_id,),
    )
    runtime._root_usd_prices = {"USD": Decimal("1"), "SOL": Decimal("2")}
    runtime._state = replace(
        runtime.state,
        bot_state=replace(
            runtime.state.bot_state,
            balances=(),
            portfolio=Portfolio(total_value_usd=ZERO_DECIMAL),
        ),
        current_prices={"SOL/USD": PriceSnapshot(price=Decimal("2"))},
    )

    runtime._build_dashboard_state(runtime.state)
    runtime._build_dashboard_state(runtime.state)

    rows = conn.execute(
        "SELECT category, pair, content, importance FROM cc_memory ORDER BY id"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "orphan_root_pruned"
    assert rows[0]["pair"] == "SOL/USD"
    assert rows[0]["importance"] == 0.5
    assert json.loads(rows[0]["content"]) == {
        "node_id": "root-sol",
        "asset": "SOL",
        "quantity_total": "50",
        "wallet_balance": "0",
        "wallet_value_usd": "0",
        "minimum_quantity": "0",
        "reason": "no matching wallet balance",
    }


def test_rotation_tree_drift_warning_threshold() -> None:
    conn = _memory_db()
    runtime = _runtime(conn=conn)
    root = RotationNode(
        node_id="root-ada",
        parent_node_id=None,
        depth=0,
        asset="ADA",
        quantity_total=Decimal("100"),
        quantity_free=Decimal("100"),
        status=RotationNodeStatus.OPEN,
    )
    runtime._rotation_tree = RotationTreeState(
        nodes=(root,),
        root_node_ids=(root.node_id,),
    )
    runtime._root_usd_prices = {"USD": Decimal("1"), "ADA": Decimal("1")}

    with patch("runtime_loop.logger.warning") as warning_mock:
        runtime._state = replace(
            runtime.state,
            bot_state=replace(
                runtime.state.bot_state,
                balances=(Balance(asset="ADA", available=Decimal("100")),),
                portfolio=Portfolio(total_value_usd=Decimal("100.40")),
            ),
            current_prices={"ADA/USD": PriceSnapshot(price=Decimal("1"))},
        )
        runtime._build_dashboard_state(runtime.state)

        drift_count = conn.execute(
            "SELECT COUNT(*) AS count FROM cc_memory WHERE category = ?",
            ("rotation_tree_drift",),
        ).fetchone()
        assert drift_count is not None
        assert drift_count["count"] == 0

        runtime._state = replace(
            runtime.state,
            bot_state=replace(
                runtime.state.bot_state,
                balances=(Balance(asset="ADA", available=Decimal("100")),),
                portfolio=Portfolio(total_value_usd=Decimal("103.00")),
            ),
            current_prices={"ADA/USD": PriceSnapshot(price=Decimal("1"))},
        )
        runtime._build_dashboard_state(runtime.state)

    rows = conn.execute(
        "SELECT category, pair, content, importance "
        "FROM cc_memory WHERE category = ? ORDER BY id",
        ("rotation_tree_drift",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "rotation_tree_drift"
    assert rows[0]["pair"] is None
    assert rows[0]["importance"] == 0.7

    content = json.loads(rows[0]["content"])
    assert content["tree_value_usd"] == "100.00"
    assert content["portfolio_total_value_usd"] == "103.00"
    assert content["delta_usd"] == "-3.00"
    assert content["roots"] == [
        {
            "node_id": "root-ada",
            "asset": "ADA",
            "status": "open",
            "quantity_total": "100",
            "price_usd": "1",
            "value_usd": "100",
        }
    ]
    assert any(
        call.args and call.args[0] == "rotation_tree_drift: %s"
        for call in warning_mock.call_args_list
    )


def _rotation_tree_drift_value(*, total_usd: str) -> RotationTreeValueResult:
    total = Decimal(total_usd)
    return RotationTreeValueResult(
        rendered_total_usd=f"{total:.2f}",
        total_usd=total,
        has_missing_prices=False,
        roots=(
            RotationTreeRootValue(
                node_id="root-ada",
                asset="ADA",
                status=RotationNodeStatus.OPEN,
                quantity_total=Decimal("100"),
                price_usd=Decimal("1"),
                value_usd=total,
            ),
        ),
    )


def _rotation_tree_drift_state(
    runtime: SchedulerRuntime, *, portfolio_total_usd: str
):
    return replace(
        runtime.state,
        bot_state=replace(
            runtime.state.bot_state,
            portfolio=Portfolio(total_value_usd=Decimal(portfolio_total_usd)),
        ),
    )


def test_rotation_tree_drift_memory_deduped_within_window() -> None:
    current_time = NOW
    runtime = _runtime()
    runtime._utc_now = lambda: current_time
    state = _rotation_tree_drift_state(runtime, portfolio_total_usd="103.00")
    tree_value = _rotation_tree_drift_value(total_usd="100")

    with patch.object(runtime._cc_memory, "_write", return_value=1) as write_mock:
        runtime._record_rotation_tree_drift(
            state=state,
            tree_value=tree_value,
            pruned_roots=(),
        )
        runtime._record_rotation_tree_drift(
            state=state,
            tree_value=tree_value,
            pruned_roots=(),
        )

    assert write_mock.call_count == 1


def test_rotation_tree_drift_memory_rewritten_after_window() -> None:
    current_time = NOW
    runtime = _runtime()
    runtime._utc_now = lambda: current_time
    state = _rotation_tree_drift_state(runtime, portfolio_total_usd="103.00")
    tree_value = _rotation_tree_drift_value(total_usd="100")

    with patch.object(runtime._cc_memory, "_write", return_value=1) as write_mock:
        runtime._record_rotation_tree_drift(
            state=state,
            tree_value=tree_value,
            pruned_roots=(),
        )
        current_time = NOW + ROTATION_TREE_DRIFT_DEDUPE_WINDOW + timedelta(seconds=1)
        runtime._record_rotation_tree_drift(
            state=state,
            tree_value=tree_value,
            pruned_roots=(),
        )

    assert write_mock.call_count == 2


def test_rotation_tree_drift_memory_rewritten_on_content_change() -> None:
    current_time = NOW
    runtime = _runtime()
    runtime._utc_now = lambda: current_time
    state = _rotation_tree_drift_state(runtime, portfolio_total_usd="103.00")

    with patch.object(runtime._cc_memory, "_write", return_value=1) as write_mock:
        runtime._record_rotation_tree_drift(
            state=state,
            tree_value=_rotation_tree_drift_value(total_usd="100"),
            pruned_roots=(),
        )
        runtime._record_rotation_tree_drift(
            state=state,
            tree_value=_rotation_tree_drift_value(total_usd="110"),
            pruned_roots=(),
        )

    assert write_mock.call_count == 2


def test_rotation_tree_drift_log_also_rate_limited(caplog) -> None:
    current_time = NOW
    runtime = _runtime()
    runtime._utc_now = lambda: current_time
    state = _rotation_tree_drift_state(runtime, portfolio_total_usd="103.00")
    tree_value = _rotation_tree_drift_value(total_usd="100")

    with patch.object(runtime._cc_memory, "_write", return_value=1):
        with caplog.at_level("WARNING", logger="runtime_loop"):
            runtime._record_rotation_tree_drift(
                state=state,
                tree_value=tree_value,
                pruned_roots=(),
            )
            assert len(caplog.records) == 1
            caplog.clear()

            runtime._record_rotation_tree_drift(
                state=state,
                tree_value=tree_value,
                pruned_roots=(),
            )

    assert len(caplog.records) == 0


async def _noop_publish(*, event: str, data, event_id: str | None = None) -> None:
    del event, data, event_id
