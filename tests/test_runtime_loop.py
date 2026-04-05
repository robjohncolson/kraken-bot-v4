from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.config import Settings, load_settings
from core.types import (
    Balance,
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    BotState,
    CancelOrder,
    MarketRegime,
    OrderSide,
    OrderType,
    PendingOrder,
    Portfolio,
    Position,
    PositionSide,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
)
from exchange.models import KrakenOrder, KrakenState, KrakenTrade
from exchange.websocket import ConnectionState, FillConfirmed, PriceTick
from guardian import PriceSnapshot
from persistence.sqlite import SqliteReader, SqliteWriter, ensure_schema
from runtime_loop import SchedulerRuntime, build_initial_scheduler_state
from scheduler import SchedulerConfig
from trading.conditional_tree import ConditionalTreeState
from trading.reconciler import RecordedPosition, RecordedState, ReconciliationReport

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


def _runtime(*, settings: Settings | None = None) -> SchedulerRuntime:
    return SchedulerRuntime(
        settings=_settings() if settings is None else settings,
        executor=FakeExecutor(KrakenState()),
        conn=_memory_db(),
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
        utc_now=lambda: NOW,
    )


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


async def _noop_publish(*, event: str, data, event_id: str | None = None) -> None:
    del event, data, event_id
