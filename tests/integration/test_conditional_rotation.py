from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from core.config import Settings, load_settings
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    BotState,
    BullCandidate,
    ClosePosition,
    MarketRegime,
    Portfolio,
    Position,
    PositionSide,
)
from exchange.models import KrakenState
from exchange.websocket import ConnectionState, FillConfirmed
from guardian import PriceSnapshot
from persistence.sqlite import ensure_schema
from runtime_loop import SchedulerRuntime
from scheduler import SchedulerConfig, SchedulerState
from trading.conditional_tree import ConditionalTreeCoordinator, ConditionalTreeState
from trading.reconciler import RecordedState

NOW = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)


class FakeExecutor:
    def __init__(self, kraken_state: KrakenState | None = None) -> None:
        self.kraken_state = KrakenState() if kraken_state is None else kraken_state
        self.orders: list[object] = []

    def fetch_kraken_state(self) -> KrakenState:
        return self.kraken_state

    def get_ws_token(self) -> str:
        return "ws-token"

    def execute_order(self, order):
        self.orders.append(order)
        return f"order-{len(self.orders)}"


class FakeRuntimeWebSocket:
    def __init__(self, on_tick, on_fill) -> None:
        self._on_tick = on_tick
        self._on_fill = on_fill
        self.state = ConnectionState.CONNECTED
        self.ticker_subscriptions: list[tuple[str, ...]] = []
        self.execution_tokens: list[str] = []

    async def connect(self) -> None:
        self.state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self.state = ConnectionState.DISCONNECTED

    async def subscribe_ticker(self, pairs) -> None:
        self.ticker_subscriptions.append(tuple(pairs))

    async def subscribe_executions(self, token: str) -> None:
        self.execution_tokens.append(token)

    async def emit_fill(self, fill: FillConfirmed) -> None:
        await self._on_fill(fill)


class FakePairScanner:
    def __init__(self, candidates: tuple[BullCandidate, ...]) -> None:
        self._candidates = candidates

    def scan_bull_candidates(self) -> tuple[BullCandidate, ...]:
        return self._candidates


def test_full_doge_bearish_rotation_flow_places_sell_then_buy_order() -> None:
    async def scenario() -> None:
        fake_websocket: FakeRuntimeWebSocket | None = None
        executor = FakeExecutor()

        def websocket_factory(on_tick, on_fill) -> FakeRuntimeWebSocket:
            nonlocal fake_websocket
            fake_websocket = FakeRuntimeWebSocket(on_tick, on_fill)
            return fake_websocket

        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=executor,
            conn=_memory_db(),
            initial_state=SchedulerState(
                bot_state=BotState(
                    portfolio=Portfolio(cash_doge=Decimal("200")),
                ),
                current_prices={"DOGE/USD": PriceSnapshot(price=Decimal("0.10"))},
                pending_belief_signals=(
                    _belief("DOGE/USD", BeliefDirection.BEARISH, 0.92),
                ),
                recorded_state=RecordedState(),
                now=NOW,
                last_reconcile_at=NOW,
            ),
            scheduler_config=SchedulerConfig(
                cycle_interval_sec=1,
                reconcile_interval_sec=9999,
                guardian_interval_sec=9999,
            ),
            websocket_factory=websocket_factory,
            conditional_tree=ConditionalTreeCoordinator(
                settings=_settings(),
                pair_scanner=FakePairScanner((_candidate("BTC/USD", Decimal("101"), peak_hours=6),)),
                ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_downtrend_closes()),
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        await runtime.run_once()

        assert len(executor.orders) == 1
        assert executor.orders[0].pair == "DOGE/USD"
        assert executor.orders[0].side.value == "sell"
        pending_sell = runtime.state.bot_state.pending_orders[0]

        assert fake_websocket is not None
        await fake_websocket.emit_fill(
            FillConfirmed(
                order_id="order-1",
                client_order_id=pending_sell.client_order_id,
                pair="DOGE/USD",
                side="sell",
                quantity=Decimal("100"),
                price=Decimal("0.10"),
                fee=Decimal("0"),
                timestamp=NOW,
            )
        )
        runtime._state = replace(runtime.state, last_reconcile_at=NOW)

        await runtime.run_once()

        assert runtime.state.pending_belief_signals == (_belief("BTC/USD", BeliefDirection.BULLISH, 0.9),)
        assert isinstance(runtime.state.current_prices["BTC/USD"], PriceSnapshot)
        assert runtime.state.conditional_tree_state is not None
        assert runtime.state.conditional_tree_state.is_active is True

        await runtime.run_once()

        assert len(executor.orders) == 2
        assert executor.orders[1].pair == "BTC/USD"
        assert executor.orders[1].side.value == "buy"
        assert ("BTC/USD",) in fake_websocket.ticker_subscriptions

    asyncio.run(scenario())


def test_no_fit_candidate_fallback_stays_in_usd() -> None:
    async def scenario() -> None:
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=FakeExecutor(),
            conn=_memory_db(),
            initial_state=SchedulerState(
                bot_state=BotState(
                    portfolio=Portfolio(cash_usd=Decimal("25")),
                    beliefs=(_belief("DOGE/USD", BeliefDirection.BEARISH, 0.9),),
                ),
                current_prices={"DOGE/USD": PriceSnapshot(price=Decimal("0.10"))},
                recorded_state=RecordedState(),
                now=NOW,
                last_reconcile_at=NOW,
            ),
            scheduler_config=SchedulerConfig(
                cycle_interval_sec=1,
                reconcile_interval_sec=9999,
                guardian_interval_sec=9999,
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(on_tick, on_fill),
            conditional_tree=ConditionalTreeCoordinator(
                settings=_settings(),
                pair_scanner=FakePairScanner((_candidate("BTC/USD", Decimal("101"), peak_hours=24),)),
                ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_mixed_bearish_closes()),
            ),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        await runtime.run_once()

        assert runtime.state.pending_belief_signals == ()
        assert runtime.state.conditional_tree_state is None

    asyncio.run(scenario())


def test_expiry_triggered_exit_closes_rotated_position_and_clears_tree() -> None:
    async def scenario() -> None:
        executor = FakeExecutor()
        runtime = SchedulerRuntime(
            settings=_settings(),
            executor=executor,
            conn=_memory_db(),
            initial_state=SchedulerState(
                bot_state=BotState(
                    portfolio=Portfolio(
                        positions=(
                            Position(
                                position_id="btc-1",
                                pair="BTC/USD",
                                side=PositionSide.LONG,
                                quantity=Decimal("0.1"),
                                entry_price=Decimal("100"),
                                stop_price=Decimal("95"),
                                target_price=Decimal("110"),
                            ),
                        ),
                    ),
                ),
                current_prices={"BTC/USD": PriceSnapshot(price=Decimal("105"))},
                conditional_tree_state=ConditionalTreeState(
                    is_active=True,
                    chosen_candidate=_candidate("BTC/USD", Decimal("100"), peak_hours=6),
                    expires_at=NOW - timedelta(minutes=1),
                ),
                now=NOW,
                last_reconcile_at=NOW,
            ),
            scheduler_config=SchedulerConfig(
                cycle_interval_sec=1,
                reconcile_interval_sec=9999,
                guardian_interval_sec=1,
            ),
            websocket_factory=lambda on_tick, on_fill: FakeRuntimeWebSocket(on_tick, on_fill),
            serve_dashboard=False,
            sse_publisher=_noop_publish,
            heartbeat_writer=lambda snapshot: None,
            utc_now=lambda: NOW,
        )

        effects = await runtime.run_once()

        assert runtime.state.bot_state.portfolio.positions == ()
        assert runtime.state.conditional_tree_state is None
        assert any(
            isinstance(effect, ClosePosition) and effect.position_id == "btc-1"
            for effect in effects
        )

    asyncio.run(scenario())


def _settings() -> Settings:
    return load_settings(
        {
            "KRAKEN_API_KEY": "key",
            "KRAKEN_API_SECRET": "secret",
            "ENABLE_CONDITIONAL_TREE": "true",
        }
    )


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


async def _noop_publish(*, event: str, data, event_id: str | None = None) -> None:
    del event, data, event_id


def _candidate(pair: str, price: Decimal, *, peak_hours: int) -> BullCandidate:
    return BullCandidate(
        pair=pair,
        belief=_belief(pair, BeliefDirection.BULLISH, 0.9),
        confidence=0.9,
        reference_price_hint=price,
        estimated_peak_hours=peak_hours,
    )


def _belief(pair: str, direction: BeliefDirection, confidence: float) -> BeliefSnapshot:
    return BeliefSnapshot(
        pair=pair,
        direction=direction,
        confidence=confidence,
        regime=MarketRegime.TRENDING,
        sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
    )


def _bars_from_close(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": close * 1.01,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1000.0,
            }
            for close in closes
        ]
    )


def _downtrend_closes() -> list[float]:
    return [120.0 - float(index) for index in range(40)]


def _mixed_bearish_closes() -> list[float]:
    closes = [100.0 for _ in range(20)]
    closes.extend([99.5, 99.0, 98.6, 98.4, 98.1, 97.9, 97.6, 97.3, 97.0, 96.8])
    closes.extend([96.7, 96.6, 96.5, 96.4, 96.3, 96.2, 96.1, 96.0, 95.9, 95.8])
    return closes
