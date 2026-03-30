from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Protocol

from fastapi.encoders import jsonable_encoder

from core.config import Settings
from core.errors import ExchangeError, KrakenBotError, SafeModeBlockedError
from core.types import (
    BeliefSnapshot,
    BeliefSource,
    BotState,
    CancelOrder,
    ClosePosition,
    FillConfirmed as CoreFillConfirmed,
    LogEvent,
    OrderRequest,
    OrderSide,
    OrderType,
    PlaceOrder,
    Position,
    PositionSide,
)
from exchange.executor import KrakenExecutor
from exchange.websocket import ConnectionState, FillConfirmed, KrakenWebSocketV2, PriceTick
from guardian import PriceSnapshot
from healing.heartbeat import HeartbeatSnapshot, HeartbeatStatus, write_heartbeat
from persistence.sqlite import SqliteReader, SqliteWriter
from scheduler import (
    BeliefRefreshRequest,
    DashboardStateUpdate,
    ReconciliationDiscrepancy,
    Scheduler,
    SchedulerConfig,
    SchedulerState,
)
from trading.conditional_tree import ConditionalTreeCoordinator, ConditionalTreeState
from trading.pair_scanner import PairScanner
from trading.reconciler import KrakenState, ReconciliationReport, RecordedState
from web.app import create_app
from web.routes import (
    BeliefEntry,
    DashboardState,
    GridPhaseCount,
    GridStatusSnapshot,
    PositionSnapshot,
    ReconciliationSnapshot,
    StrategyStatsSnapshot,
    create_router,
)
from web.sse import publish

logger = logging.getLogger(__name__)

DEFAULT_CYCLE_INTERVAL_SEC = 30
DEFAULT_GUARDIAN_INTERVAL_SEC = 120

SsePublisher = Callable[..., Awaitable[None]]
HeartbeatWriter = Callable[[HeartbeatSnapshot], None]
Sleep = Callable[[float], Awaitable[None]]
UtcNow = Callable[[], datetime]


class SupportsRuntimeWebSocket(Protocol):
    state: ConnectionState

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def subscribe_ticker(self, pairs: Iterable[str]) -> None: ...
    async def subscribe_executions(self, token: str) -> None: ...


WebSocketFactory = Callable[
    [Callable[[PriceTick], Awaitable[None]], Callable[[FillConfirmed], Awaitable[None]]],
    SupportsRuntimeWebSocket,
]
BeliefRefreshHandler = Callable[
    [BeliefRefreshRequest],
    BeliefSnapshot | None | Awaitable[BeliefSnapshot | None],
]


@dataclass(slots=True)
class DashboardStateStore:
    _state: DashboardState
    _lock: Lock = field(default_factory=Lock)

    def snapshot(self) -> DashboardState:
        with self._lock:
            return self._state

    def update(self, state: DashboardState) -> None:
        with self._lock:
            self._state = state


def build_runtime_app(*, state_provider: Callable[[], DashboardState]):
    application = create_app()
    application.include_router(create_router(state_provider=state_provider))
    return application


def build_initial_scheduler_state(
    *,
    kraken_state: KrakenState,
    recorded_state: RecordedState,
    report: ReconciliationReport,
    now: datetime | None = None,
) -> SchedulerState:
    effective_now = _utcnow() if now is None else _normalize_timestamp(now)
    from core.types import Portfolio, ZERO_DECIMAL

    usd = sum(
        (b.available + b.held for b in kraken_state.balances if b.asset == "USD"),
        start=ZERO_DECIMAL,
    )
    doge = sum(
        (b.available + b.held for b in kraken_state.balances if b.asset == "DOGE"),
        start=ZERO_DECIMAL,
    )
    portfolio = Portfolio(cash_usd=usd, cash_doge=doge)

    return SchedulerState(
        bot_state=BotState(balances=kraken_state.balances, portfolio=portfolio),
        kraken_state=kraken_state,
        recorded_state=recorded_state,
        now=effective_now,
        last_reconcile_at=effective_now,
        last_reconciliation_report=report,
    )


class SchedulerRuntime:
    """Async runtime that bridges WebSocket events into the pure scheduler."""

    def __init__(
        self,
        *,
        settings: Settings,
        executor: KrakenExecutor,
        conn: sqlite3.Connection,
        initial_state: SchedulerState,
        scheduler: Scheduler | None = None,
        scheduler_config: SchedulerConfig | None = None,
        websocket_factory: WebSocketFactory | None = None,
        belief_refresh_handler: BeliefRefreshHandler | None = None,
        conditional_tree: ConditionalTreeCoordinator | None = None,
        serve_dashboard: bool = True,
        sse_publisher: SsePublisher = publish,
        heartbeat_writer: HeartbeatWriter = write_heartbeat,
        sleep: Sleep = asyncio.sleep,
        utc_now: UtcNow | None = None,
    ) -> None:
        self._settings = settings
        self._executor = executor
        self._reader = SqliteReader(conn)
        self._writer = SqliteWriter(conn)
        self._scheduler_config = scheduler_config or SchedulerConfig(
            cycle_interval_sec=DEFAULT_CYCLE_INTERVAL_SEC,
            reconcile_interval_sec=settings.reconcile_interval_sec,
            guardian_interval_sec=DEFAULT_GUARDIAN_INTERVAL_SEC,
        )
        self._scheduler = scheduler or Scheduler(
            config=self._scheduler_config,
            settings=settings,
        )
        self._belief_refresh_handler = belief_refresh_handler
        self._conditional_tree_state = ConditionalTreeState()
        self._conditional_tree = None
        if settings.enable_conditional_tree:
            self._conditional_tree = conditional_tree or _build_conditional_tree(
                settings=settings,
                executor=executor,
            )
        self._sleep = sleep
        self._utc_now = utc_now or _utcnow
        self._sse_publisher = sse_publisher
        self._heartbeat_writer = heartbeat_writer
        self._state = replace(initial_state, now=_normalize_timestamp(initial_state.now))
        self._state_lock = asyncio.Lock()
        self._belief_timestamps: dict[str, datetime] = {}
        self._dashboard_store = DashboardStateStore(self._build_dashboard_state(self._state))
        self._serve_dashboard = serve_dashboard
        self._dashboard_server = None
        self._dashboard_task: asyncio.Task[None] | None = None
        self._dashboard_event_id = 0
        self._subscribed_pairs: set[str] = set()
        self._execution_feed_ready = False
        self._last_runtime_error: str | None = None
        self._last_belief_poll_at: datetime | None = None
        self._belief_poll_interval_sec = settings.belief_stale_hours * 3600 // 2  # poll at half staleness
        factory = websocket_factory or _default_websocket_factory
        self._websocket = factory(self._handle_price_tick, self._handle_fill_confirmed)
        self.app = build_runtime_app(state_provider=self._dashboard_store.snapshot)

    @property
    def state(self) -> SchedulerState:
        return self._state

    async def start(self) -> None:
        if self._serve_dashboard:
            await self._start_dashboard_server()
        await self._ensure_websocket_connected()
        await self._ensure_subscriptions()
        await self._publish_dashboard_update()
        self._write_heartbeat()

    async def shutdown(self) -> None:
        if self._dashboard_server is not None:
            self._dashboard_server.should_exit = True
        if self._dashboard_task is not None:
            await self._dashboard_task
            self._dashboard_task = None
            self._dashboard_server = None
        await self._websocket.disconnect()

    async def run_forever(self) -> None:
        await self.start()
        try:
            while True:
                await self.run_once()
                await self._sleep(self._scheduler_config.cycle_interval_sec)
        finally:
            await self.shutdown()

    async def run_once(self) -> tuple[object, ...]:
        now = self._utc_now()
        try:
            async with self._state_lock:
                state = replace(self._state, now=now)
                if _interval_due(
                    state.last_reconcile_at,
                    now,
                    self._scheduler_config.reconcile_interval_sec,
                ):
                    state = replace(
                        state,
                        kraken_state=self._executor.fetch_kraken_state(),
                        recorded_state=self._reader.fetch_recorded_state(),
                    )
                new_state, effects = self._scheduler.run_cycle(state)
                self._state = new_state
            self._last_runtime_error = None
        except (ExchangeError, KrakenBotError) as exc:
            self._last_runtime_error = str(exc)
            logger.error("Scheduler runtime cycle failed: %s", exc)
            self._write_heartbeat()
            return ()

        await self._ensure_websocket_connected()
        await self._ensure_subscriptions()
        await self._maybe_poll_beliefs(now)
        await self._handle_effects(effects)
        await self._maybe_plan_conditional_rotation(now)
        self._write_heartbeat()
        return effects

    async def enqueue_belief(
        self,
        belief: BeliefSnapshot,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        timestamp = self._utc_now() if observed_at is None else _normalize_timestamp(observed_at)
        async with self._state_lock:
            pending = self._state.pending_belief_signals + (belief,)
            self._state = replace(self._state, pending_belief_signals=pending)
        self._belief_timestamps[belief.pair] = timestamp

    async def _handle_price_tick(self, tick: PriceTick) -> None:
        belief_timestamp = self._belief_timestamps.get(tick.pair)
        async with self._state_lock:
            current_prices = dict(self._state.current_prices)
            existing = current_prices.get(tick.pair)
            if isinstance(existing, PriceSnapshot) and existing.belief_timestamp is not None:
                belief_timestamp = existing.belief_timestamp
            current_prices[tick.pair] = PriceSnapshot(
                price=tick.last,
                belief_timestamp=belief_timestamp,
            )
            self._state = replace(self._state, current_prices=current_prices)

    async def _handle_fill_confirmed(self, fill: FillConfirmed) -> None:
        self._writer.insert_ledger_entry(
            fill.pair,
            fill.side,
            fill.quantity,
            fill.price,
            fill.fee,
            _render_timestamp(fill.timestamp),
        )
        async with self._state_lock:
            self._state = replace(
                self._state,
                kraken_state=self._executor.fetch_kraken_state(),
                recorded_state=self._reader.fetch_recorded_state(),
                last_reconcile_at=None,
            )
        # Enqueue core FillConfirmed for reducer processing on next cycle
        core_fill = CoreFillConfirmed(
            order_id=fill.order_id,
            pair=fill.pair,
            filled_quantity=fill.quantity,
            fill_price=fill.price,
            client_order_id=fill.client_order_id,
        )
        async with self._state_lock:
            pending = self._state.pending_fills + (core_fill,)
            self._state = replace(self._state, pending_fills=pending)

    async def _handle_effects(self, effects: tuple[object, ...]) -> None:
        for effect in effects:
            if isinstance(effect, PlaceOrder):
                await self._execute_place_order(effect)
                continue
            if isinstance(effect, CancelOrder):
                await self._execute_cancel_order(effect)
                continue
            if isinstance(effect, ClosePosition):
                await self._execute_close_position(effect)
                continue
            if isinstance(effect, LogEvent):
                logger.info("Reducer: %s", effect.message)
                continue
            if isinstance(effect, BeliefRefreshRequest):
                await self._maybe_refresh_belief(effect)
                continue
            if isinstance(effect, ReconciliationDiscrepancy):
                logger.warning("Reconciliation discrepancy detected: %s", effect.summary)
                continue
            if isinstance(effect, DashboardStateUpdate):
                await self._publish_dashboard_update()

    async def _execute_place_order(self, effect: PlaceOrder) -> None:
        try:
            order_id = self._executor.execute_order(effect.order)
            logger.info("Placed order %s for %s", order_id, effect.order.pair)
        except (ExchangeError, SafeModeBlockedError) as exc:
            logger.error("Failed to place order for %s: %s", effect.order.pair, exc)

    async def _execute_cancel_order(self, effect: CancelOrder) -> None:
        cancel_target = effect.order_id or effect.client_order_id
        try:
            if effect.order_id:
                self._executor.execute_cancel(effect.order_id)
            logger.info("Canceled order %s", cancel_target)
        except (ExchangeError, SafeModeBlockedError) as exc:
            logger.error("Failed to cancel order %s: %s", cancel_target, exc)

    async def _execute_close_position(self, effect: ClosePosition) -> None:
        position = self._find_position(effect.position_id)
        if position is None:
            logger.warning(
                "Close position %s (%s): position not found in portfolio",
                effect.position_id, effect.reason,
            )
            return
        close_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
        order = OrderRequest(
            pair=position.pair,
            side=close_side,
            order_type=OrderType.LIMIT,
            quantity=position.quantity,
            limit_price=position.entry_price,  # placeholder; market conditions may differ
        )
        try:
            order_id = self._executor.execute_order(order)
            logger.info(
                "Close position %s (%s): placed %s order %s for %s qty=%s",
                effect.position_id, effect.reason, close_side.value,
                order_id, position.pair, position.quantity,
            )
        except (ExchangeError, SafeModeBlockedError) as exc:
            logger.error(
                "Close position %s (%s): failed to place closing order: %s",
                effect.position_id, effect.reason, exc,
            )

    def _find_position(self, position_id: str) -> Position | None:
        for p in self._state.bot_state.portfolio.positions:
            if p.position_id == position_id:
                return p
        return None

    async def _maybe_poll_beliefs(self, now: datetime) -> None:
        """Periodically generate beliefs for allowed pairs (cold-start + refresh)."""
        if self._belief_refresh_handler is None:
            return
        if (
            self._last_belief_poll_at is not None
            and (now - self._last_belief_poll_at).total_seconds() < self._belief_poll_interval_sec
        ):
            return

        self._last_belief_poll_at = now
        allowed_pairs = self._settings.allowed_pairs
        if not allowed_pairs:
            return

        for pair in sorted(allowed_pairs):
            dummy_request = BeliefRefreshRequest(
                pair=pair,
                position_id="",
                checked_at=now,
                stale_after_hours=self._settings.belief_stale_hours,
            )
            result = self._belief_refresh_handler(dummy_request)
            belief = await result if inspect.isawaitable(result) else result
            if belief is not None:
                await self.enqueue_belief(belief, observed_at=now)

    async def _maybe_refresh_belief(self, request: BeliefRefreshRequest) -> None:
        if self._belief_refresh_handler is None:
            logger.info(
                "Belief refresh requested for %s but no handler is configured",
                request.pair,
            )
            return

        result = self._belief_refresh_handler(request)
        belief = await result if inspect.isawaitable(result) else result
        if belief is None:
            logger.info("Belief refresh handler returned no snapshot for %s", request.pair)
            return
        await self.enqueue_belief(belief, observed_at=request.checked_at)

    async def _maybe_plan_conditional_rotation(self, now: datetime) -> None:
        if self._conditional_tree is None or self._conditional_tree_state.is_active:
            return

        async with self._state_lock:
            state_snapshot = self._state
            tree_state = self._conditional_tree_state

        planned_state = self._conditional_tree.maybe_plan(
            state=state_snapshot,
            tree_state=tree_state,
            now=now,
        )
        if planned_state is None or planned_state.chosen_candidate is None:
            return

        candidate = planned_state.chosen_candidate
        await self._seed_candidate_reference_price(
            pair=candidate.pair,
            reference_price=candidate.reference_price_hint,
            observed_at=now,
        )
        await self._subscribe_candidate_pair(candidate.pair)

        async with self._state_lock:
            self._conditional_tree_state = planned_state

        await self.enqueue_belief(candidate.belief, observed_at=now)

    async def _seed_candidate_reference_price(
        self,
        *,
        pair: str,
        reference_price: Decimal,
        observed_at: datetime,
    ) -> None:
        async with self._state_lock:
            if pair in self._state.current_prices:
                return
            current_prices = dict(self._state.current_prices)
            current_prices[pair] = PriceSnapshot(
                price=reference_price,
                belief_timestamp=observed_at,
            )
            self._state = replace(self._state, current_prices=current_prices)

    async def _subscribe_candidate_pair(self, pair: str) -> None:
        if self._websocket.state is not ConnectionState.CONNECTED:
            return
        if pair in self._subscribed_pairs:
            return
        try:
            await self._websocket.subscribe_ticker((pair,))
        except ExchangeError as exc:
            logger.warning("Ticker subscription failed for %s: %s", pair, exc)
            self._last_runtime_error = str(exc)
        else:
            self._subscribed_pairs.add(pair)

    async def _publish_dashboard_update(self) -> None:
        dashboard_state = self._build_dashboard_state(self._state)
        self._dashboard_store.update(dashboard_state)
        self._dashboard_event_id += 1
        await self._sse_publisher(
            event="dashboard.update",
            data={
                "portfolio": jsonable_encoder(dashboard_state.portfolio),
                "positions": {"positions": jsonable_encoder(dashboard_state.positions)},
                "grid": jsonable_encoder(dashboard_state.grids),
                "beliefs": jsonable_encoder(dashboard_state.beliefs),
                "stats": jsonable_encoder(dashboard_state.stats),
                "reconciliation": jsonable_encoder(dashboard_state.reconciliation),
            },
            event_id=f"dashboard-{self._dashboard_event_id}",
        )

    async def _start_dashboard_server(self) -> None:
        if self._dashboard_task is not None:
            return

        import uvicorn

        config = uvicorn.Config(
            self.app,
            host=self._settings.web_host,
            port=self._settings.web_port,
            log_level="info",
            access_log=False,
        )
        self._dashboard_server = uvicorn.Server(config)
        self._dashboard_task = asyncio.create_task(
            self._dashboard_server.serve(),
            name="kraken-dashboard",
        )
        await self._sleep(0)

    async def _ensure_websocket_connected(self) -> None:
        if self._websocket.state is not ConnectionState.DISCONNECTED:
            return
        try:
            await self._websocket.connect()
        except ExchangeError as exc:
            logger.warning("Kraken WebSocket connect failed: %s", exc)
            self._last_runtime_error = str(exc)

    async def _ensure_subscriptions(self) -> None:
        if self._websocket.state is not ConnectionState.CONNECTED:
            return

        active_pairs = sorted(
            _active_pairs(self._state) | self._settings.allowed_pairs
        )
        new_pairs = [pair for pair in active_pairs if pair not in self._subscribed_pairs]
        if new_pairs:
            try:
                await self._websocket.subscribe_ticker(new_pairs)
            except ExchangeError as exc:
                logger.warning("Ticker subscription failed: %s", exc)
                self._last_runtime_error = str(exc)
            else:
                self._subscribed_pairs.update(new_pairs)

        if self._execution_feed_ready:
            return
        try:
            token = self._executor.get_ws_token()
            await self._websocket.subscribe_executions(token)
        except ExchangeError as exc:
            logger.warning("Execution feed subscription failed: %s", exc)
            self._last_runtime_error = str(exc)
        else:
            self._execution_feed_ready = True

    def _write_heartbeat(self) -> None:
        now = self._utc_now()
        snapshot = HeartbeatSnapshot(
            timestamp=now,
            bot_status=self._heartbeat_status(),
            active_positions_count=len(self._state.bot_state.portfolio.positions),
            open_orders_count=len(self._state.bot_state.open_orders),
            last_reconciliation_age_sec=_age_seconds(self._state.last_reconcile_at, now),
            last_belief_age_sec=_belief_age_seconds(self._belief_timestamps.values(), now),
            websocket_connected=self._websocket.state is ConnectionState.CONNECTED,
            persistence_connected=True,
        )
        self._heartbeat_writer(snapshot)

    def _heartbeat_status(self) -> HeartbeatStatus:
        if self._last_runtime_error:
            return HeartbeatStatus.DEGRADED
        if self._websocket.state is not ConnectionState.CONNECTED:
            return HeartbeatStatus.DEGRADED
        return HeartbeatStatus.HEALTHY

    def _build_dashboard_state(self, state: SchedulerState) -> DashboardState:
        return DashboardState(
            portfolio=state.bot_state.portfolio,
            positions=_build_position_snapshots(
                state.bot_state.portfolio.positions,
                state.current_prices,
            ),
            grids=_build_grid_snapshots(state.bot_state.portfolio.positions),
            beliefs=_build_belief_entries(state.bot_state.beliefs),
            stats=StrategyStatsSnapshot(),
            reconciliation=ReconciliationSnapshot(
                checked_at=state.last_reconcile_at,
                report=state.last_reconciliation_report,
            ),
        )


def _default_websocket_factory(
    ticker_handler: Callable[[PriceTick], Awaitable[None]],
    fill_handler: Callable[[FillConfirmed], Awaitable[None]],
) -> SupportsRuntimeWebSocket:
    return KrakenWebSocketV2(
        ticker_handler=ticker_handler,
        fill_handler=fill_handler,
    )


def _build_conditional_tree(
    *,
    settings: Settings,
    executor: KrakenExecutor,
) -> ConditionalTreeCoordinator | None:
    client = getattr(executor, "_client", None)
    if client is None:
        logger.warning("Conditional tree enabled but executor client is unavailable")
        return None
    return ConditionalTreeCoordinator(
        settings=settings,
        pair_scanner=PairScanner(client=client, settings=settings),
    )


def _build_position_snapshots(
    positions: tuple[Position, ...],
    current_prices: dict[str, Decimal | PriceSnapshot],
) -> tuple[PositionSnapshot, ...]:
    snapshots: list[PositionSnapshot] = []
    for position in positions:
        price = position.entry_price
        current = current_prices.get(position.pair)
        if isinstance(current, PriceSnapshot):
            price = current.price
        elif isinstance(current, Decimal):
            price = current
        snapshots.append(
            PositionSnapshot(
                position=position,
                current_price=price,
                unrealized_pnl_usd=_unrealized_pnl(position, price),
            )
        )
    return tuple(snapshots)


def _build_grid_snapshots(positions: tuple[Position, ...]) -> tuple[GridStatusSnapshot, ...]:
    phase_counts_by_pair: dict[str, dict[object, int]] = {}
    active_slots_by_pair: dict[str, int] = {}

    for position in positions:
        if position.grid_state is None:
            continue
        pair_counts = phase_counts_by_pair.setdefault(position.pair, {})
        pair_counts[position.grid_state.phase] = (
            pair_counts.get(position.grid_state.phase, 0)
            + position.grid_state.active_slot_count
        )
        active_slots_by_pair[position.pair] = (
            active_slots_by_pair.get(position.pair, 0)
            + position.grid_state.active_slot_count
        )

    snapshots: list[GridStatusSnapshot] = []
    for pair in sorted(active_slots_by_pair):
        phase_distribution = tuple(
            GridPhaseCount(phase=phase, active_slots=count)
            for phase, count in sorted(
                phase_counts_by_pair[pair].items(),
                key=lambda item: item[0].value,
            )
        )
        snapshots.append(
            GridStatusSnapshot(
                pair=pair,
                active_slots=active_slots_by_pair[pair],
                phase_distribution=phase_distribution,
            )
        )
    return tuple(snapshots)


def _build_belief_entries(
    beliefs: tuple[BeliefSnapshot, ...],
) -> tuple[BeliefEntry, ...]:
    entries: list[BeliefEntry] = []
    for belief in beliefs:
        for source in belief.sources:
            entries.append(
                BeliefEntry(
                    pair=belief.pair,
                    source=source if isinstance(source, BeliefSource) else BeliefSource(str(source)),
                    direction=belief.direction,
                    confidence=belief.confidence,
                    regime=belief.regime,
                )
            )
    return tuple(sorted(entries, key=lambda item: (item.pair, item.source.value)))


def _active_pairs(state: SchedulerState) -> set[str]:
    pairs = {
        position.pair for position in state.bot_state.portfolio.positions
    }
    pairs.update(order.pair for order in state.recorded_state.orders)
    pairs.update(position.pair for position in state.recorded_state.positions)
    pairs.update(order.pair for order in state.kraken_state.open_orders)
    pairs.update(belief.pair for belief in state.bot_state.beliefs)
    pairs.update(state.current_prices.keys())
    return {pair for pair in pairs if pair}


def _unrealized_pnl(position: Position, current_price: Decimal) -> Decimal:
    if position.side is PositionSide.LONG:
        return (current_price - position.entry_price) * position.quantity
    return (position.entry_price - current_price) * position.quantity


def _interval_due(
    last_run_at: datetime | None,
    now: datetime,
    interval_seconds: int,
) -> bool:
    if last_run_at is None:
        return True
    return now - _normalize_timestamp(last_run_at) >= timedelta(seconds=interval_seconds)


def _age_seconds(timestamp: datetime | None, now: datetime) -> float:
    if timestamp is None:
        return 0.0
    return max(0.0, (now - _normalize_timestamp(timestamp)).total_seconds())


def _belief_age_seconds(timestamps: Iterable[datetime], now: datetime) -> float:
    normalized = [_normalize_timestamp(timestamp) for timestamp in timestamps]
    if not normalized:
        return 0.0
    return max(max(0.0, (now - timestamp).total_seconds()) for timestamp in normalized)


def _render_timestamp(value: datetime) -> str:
    return _normalize_timestamp(value).isoformat().replace("+00:00", "Z")


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "DashboardStateStore",
    "DEFAULT_CYCLE_INTERVAL_SEC",
    "DEFAULT_GUARDIAN_INTERVAL_SEC",
    "SchedulerRuntime",
    "build_initial_scheduler_state",
    "build_runtime_app",
]
