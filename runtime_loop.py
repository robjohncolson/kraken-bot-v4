from __future__ import annotations

import asyncio
import inspect
import logging
import sqlite3
import time as _time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Protocol

from fastapi.encoders import jsonable_encoder

from core.config import Settings, validate_settings
from core.errors import (
    ExchangeError,
    InsufficientFundsError,
    KrakenBotError,
    RateLimitExceededError,
    SafeModeBlockedError,
)
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
    PendingOrder,
    PlaceOrder,
    Position,
    PositionSide,
    RotationEvent,
    RotationNodeStatus,
    RotationTreeState,
    ZERO_DECIMAL,
)
from collections import deque

from exchange.executor import KrakenExecutor
from exchange.pair_metadata import PairMetadataCache
from grid.sizing import set_pair_metadata_cache
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
from trading.rotation_planner import RotationTreePlanner
from trading.rotation_tree import (
    cancel_planned_node,
    cascade_close,
    destination_quantity,
    entry_base_quantity,
    exit_base_quantity,
    exit_proceeds,
    expired_nodes,
    node_by_id,
    update_node,
)
from trading.reconciler import KrakenState, ReconciliationReport, RecordedState
from web.app import create_app
from web.routes import (
    BeliefEntry,
    DashboardState,
    GridPhaseCount,
    GridStatusSnapshot,
    PositionSnapshot,
    ReconciliationSnapshot,
    RotationNodeSnapshot,
    RotationTreeSnapshot,
    StrategyStatsSnapshot,
    create_router,
)
from web.sse import publish

logger = logging.getLogger(__name__)

DEFAULT_CYCLE_INTERVAL_SEC = 30
DEFAULT_GUARDIAN_INTERVAL_SEC = 120
ROTATION_ENTRY_MAX_RETRIES = 3
ROTATION_PAIR_COOLDOWN_SEC = 1800  # 30 min cooldown after cancel

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
ShadowBeliefHandler = Callable[[BeliefRefreshRequest], None]


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
    persisted_positions: tuple[Position, ...] = (),
    persisted_pending_orders: tuple[tuple[PendingOrder, str | None], ...] = (),
    persisted_cooldowns: tuple[tuple[str, str], ...] = (),
) -> SchedulerState:
    effective_now = _utcnow() if now is None else _normalize_timestamp(now)
    from core.types import Portfolio

    usd = sum(
        (b.available + b.held for b in kraken_state.balances if b.asset == "USD"),
        start=ZERO_DECIMAL,
    )
    doge = sum(
        (b.available + b.held for b in kraken_state.balances if b.asset == "DOGE"),
        start=ZERO_DECIMAL,
    )
    portfolio = Portfolio(
        cash_usd=usd,
        cash_doge=doge,
        positions=persisted_positions,
    )

    # Derive next_position_seq from existing position IDs (format: kbv4-slug-000001)
    seq = 0
    for pos in persisted_positions:
        if pos.position_id.startswith("kbv4-"):
            try:
                seq = max(seq, int(pos.position_id.rsplit("-", 1)[-1]))
            except (IndexError, ValueError):
                pass

    # Filter pending orders against Kraken open orders
    # Match by client_order_id OR by exchange_order_id stored in the DB
    exchange_coids = {o.client_order_id for o in kraken_state.open_orders if o.client_order_id}
    exchange_oids = {o.order_id for o in kraken_state.open_orders}
    # fetch_open_orders returns (PendingOrder, exchange_order_id) pairs
    live_pending = tuple(
        po for po, exch_oid in persisted_pending_orders
        if po.client_order_id in exchange_coids or (exch_oid and exch_oid in exchange_oids)
    )

    return SchedulerState(
        bot_state=BotState(
            balances=kraken_state.balances,
            portfolio=portfolio,
            pending_orders=live_pending,
            cooldowns=persisted_cooldowns,
            next_position_seq=seq + 1,
        ),
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
        shadow_belief_handler: ShadowBeliefHandler | None = None,
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
        self._shadow_belief_handler = shadow_belief_handler
        self._conditional_tree_state = _conditional_tree_state(initial_state)
        self._conditional_tree = None
        if settings.enable_conditional_tree:
            self._conditional_tree = conditional_tree or _build_conditional_tree(
                settings=settings,
                executor=executor,
            )
        # Pair metadata cache (ordermin enforcement)
        self._pair_metadata = PairMetadataCache(conn)
        self._pair_metadata.load_from_db()
        set_pair_metadata_cache(self._pair_metadata)
        # Rotation tree (denomination-agnostic recursive trading)
        self._rotation_tree: RotationTreeState | None = None
        self._rotation_planner: RotationTreePlanner | None = None
        self._rotation_fill_queue: list[tuple[str, Decimal, Decimal, str]] = []
        self._rotation_entry_retry_counts: dict[str, int] = {}
        self._rotation_pair_cooldowns: dict[str, float] = {}  # pair → monotonic expiry
        if settings.enable_rotation_tree:
            scanner = PairScanner(client=executor._client, settings=settings)
            self._rotation_planner = RotationTreePlanner(
                settings=settings, pair_scanner=scanner,
                pair_metadata=self._pair_metadata,
            )
            # Initialize root nodes from current balances
            balances_dict = {
                b.asset: b.available + b.held
                for b in initial_state.kraken_state.balances
            }
            prices = _collect_root_prices(initial_state.current_prices, balances_dict)
            self._rotation_tree = self._rotation_planner.initialize_roots(
                balances_dict, prices_usd=prices,
            )
            logger.info(
                "Rotation tree initialized: %d root nodes",
                len(self._rotation_tree.root_node_ids),
            )

        self._runtime_started_at = datetime.now(timezone.utc)
        self._sleep = sleep
        self._utc_now = utc_now or _utcnow
        self._sse_publisher = sse_publisher
        self._heartbeat_writer = heartbeat_writer
        self._state = replace(initial_state, now=_normalize_timestamp(initial_state.now))
        self._state_lock = asyncio.Lock()
        self._belief_timestamps: dict[str, datetime] = {}
        # All beliefs for display (including low-confidence / filtered ones)
        self._display_beliefs: dict[str, BeliefSnapshot] = {}
        # Rotation event log (capped ring buffer)
        self._rotation_events: deque[RotationEvent] = deque(maxlen=100)
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
        self._ws_backoff_until: datetime | None = None
        factory = websocket_factory or _default_websocket_factory
        self._websocket = factory(self._handle_price_tick, self._handle_fill_confirmed)
        self.app = build_runtime_app(state_provider=self._dashboard_store.snapshot)

    @property
    def state(self) -> SchedulerState:
        return self._state

    async def start(self) -> None:
        # Validate settings and log warnings at startup
        for warning in validate_settings(self._settings):
            logger.warning("Settings validation: %s", warning)
        # Refresh pair metadata (ordermin) from Kraken API
        if self._pair_metadata.stale(max_age_hours=self._settings.pair_metadata_refresh_hours):
            self._pair_metadata.refresh()
            logger.info("Pair metadata refreshed: %d pairs cached", self._pair_metadata.pair_count)
        if self._serve_dashboard:
            await self._start_dashboard_server()
        # WS connect is deferred to run_once — skip here to avoid blocking
        # startup on flaky/filtered networks. REST price fallback covers us.
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
        logger.info("cycle_start: %s", now.isoformat())
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
                self._conditional_tree_state = _conditional_tree_state(new_state)
            self._last_runtime_error = None
        except (ExchangeError, KrakenBotError) as exc:
            self._last_runtime_error = str(exc)
            logger.error("Scheduler runtime cycle failed: %s", exc)
            self._write_heartbeat()
            return ()

        await self._ensure_websocket_connected()
        await self._ensure_subscriptions()
        await self._maybe_bind_tree_to_position()
        await self._persist_state_changes(state, new_state)
        await self._maybe_poll_beliefs(now)
        await self._handle_effects(effects)
        await self._maybe_plan_conditional_rotation(now)
        await self._maybe_run_rotation_planner(now)
        self._write_heartbeat()
        return effects

    async def enqueue_belief(
        self,
        belief: BeliefSnapshot,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        timestamp = self._utc_now() if observed_at is None else _normalize_timestamp(observed_at)
        # Always stash for dashboard display (TUI shows all beliefs)
        self._display_beliefs[belief.pair] = belief
        # Confidence gate: drop low-confidence beliefs from trading decisions
        if belief.confidence < self._settings.min_belief_confidence:
            logger.debug(
                "Dropping low-confidence belief for %s (%.2f < %.2f)",
                belief.pair, belief.confidence, self._settings.min_belief_confidence,
            )
            # Still update timestamp so guardian staleness detection works
            self._belief_timestamps[belief.pair] = timestamp
            return
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
        # Stash rotation fill details before the reducer consumes the PendingOrder
        rotation_po = next(
            (po for po in self._state.bot_state.pending_orders
             if po.rotation_node_id
             and po.client_order_id == fill.client_order_id),
            None,
        )
        if rotation_po:
            self._rotation_fill_queue.append((
                rotation_po.rotation_node_id,
                fill.quantity,
                fill.price,
                rotation_po.kind,
            ))

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
        # Mark the tracked order as filled in persistence
        if fill.order_id:
            try:
                self._writer.close_order(fill.order_id)
            except Exception:
                logger.debug("Could not close tracked order %s", fill.order_id)

    async def _maybe_bind_tree_to_position(self) -> None:
        """Bind conditional tree to its opened position after reducer creates it."""
        tree = self._conditional_tree_state
        if not tree.is_active or tree.position_id is not None:
            return
        if tree.chosen_candidate is None:
            return

        candidate_pair = tree.chosen_candidate.pair
        position = next(
            (p for p in self._state.bot_state.portfolio.positions if p.pair == candidate_pair),
            None,
        )
        if position is None:
            return

        now = self._utc_now()
        window = timedelta(hours=tree.planned_window_hours)
        updated_tree = replace(
            tree,
            position_id=position.position_id,
            opened_at=now,
            expires_at=now + window,
            exit_deadline=now + window,
        )
        async with self._state_lock:
            self._conditional_tree_state = updated_tree
            self._state = replace(self._state, conditional_tree_state=updated_tree)

    async def _persist_state_changes(
        self, old_state: SchedulerState, new_state: SchedulerState,
    ) -> None:
        """Diff old vs new bot state and persist changes to SQLite."""
        old_positions = {p.position_id: p for p in old_state.bot_state.portfolio.positions}
        new_positions = {p.position_id: p for p in new_state.bot_state.portfolio.positions}

        # Persist new or updated positions
        for pid, pos in new_positions.items():
            old_pos = old_positions.get(pid)
            if old_pos is None or old_pos != pos:
                try:
                    self._writer.upsert_position(pos)
                except Exception:
                    logger.debug("Failed to persist position %s", pid)

        # Close removed positions
        for pid in old_positions:
            if pid not in new_positions:
                try:
                    self._writer.update_position_closed(pid)
                except Exception:
                    logger.debug("Failed to close position %s", pid)

        # Persist cooldown changes
        old_cooldowns = set(old_state.bot_state.cooldowns)
        new_cooldowns = set(new_state.bot_state.cooldowns)
        for pair, ts in new_cooldowns - old_cooldowns:
            try:
                self._writer.set_cooldown(pair, ts)
            except Exception:
                logger.debug("Failed to persist cooldown for %s", pair)

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
            # Find matching PendingOrder from bot state for rich metadata
            pending = next(
                (po for po in self._state.bot_state.pending_orders
                 if po.client_order_id == getattr(effect.order, "client_order_id", "")),
                None,
            )
            self._writer.upsert_order(
                order_id=order_id,
                pair=effect.order.pair,
                client_order_id=pending.client_order_id if pending else order_id,
                kind=pending.kind if pending else "position_entry",
                side=pending.side if pending else effect.order.side.value,
                base_qty=pending.base_qty if pending else effect.order.quantity,
                filled_qty=pending.filled_qty if pending else ZERO_DECIMAL,
                quote_qty=pending.quote_qty if pending else ZERO_DECIMAL,
                limit_price=effect.order.limit_price,
                position_id=pending.position_id if pending else None,
                exchange_order_id=order_id,
                rotation_node_id=pending.rotation_node_id if pending else None,
            )
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
        if not effect.pair or effect.quantity <= 0:
            logger.warning(
                "Close position %s (%s): effect missing pair/quantity",
                effect.position_id, effect.reason,
            )
            return
        close_side = OrderSide.SELL if effect.side == PositionSide.LONG else OrderSide.BUY
        raw_price = effect.limit_price or ZERO_DECIMAL
        limit_price = _apply_exit_offset(
            raw_price, effect.side, self._settings.exit_limit_offset_pct,
        )
        order = OrderRequest(
            pair=effect.pair,
            side=close_side,
            order_type=OrderType.LIMIT,
            quantity=effect.quantity,
            limit_price=limit_price,
        )
        try:
            order_id = self._executor.execute_order(order)
            logger.info(
                "Close position %s (%s): placed %s order %s for %s qty=%s",
                effect.position_id, effect.reason, close_side.value,
                order_id, effect.pair, effect.quantity,
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

            # REST price fallback: if WS hasn't delivered a price yet,
            # use the latest OHLCV close so the reducer has a reference price.
            async with self._state_lock:
                if pair not in self._state.current_prices:
                    try:
                        from exchange.ohlcv import fetch_ohlcv
                        bars = fetch_ohlcv(pair, interval=60, count=1)
                        if not bars.empty:
                            last_close = Decimal(str(float(bars["close"].iloc[-1])))
                            prices = dict(self._state.current_prices)
                            prices[pair] = PriceSnapshot(
                                price=last_close,
                                belief_timestamp=self._belief_timestamps.get(pair),
                            )
                            self._state = replace(self._state, current_prices=prices)
                            logger.info("REST price fallback for %s: %s", pair, last_close)
                    except Exception:
                        logger.warning("REST price fallback failed for %s", pair, exc_info=True)

            # Shadow: log research model prediction without enqueueing
            if self._shadow_belief_handler is not None:
                try:
                    self._shadow_belief_handler(dummy_request)
                except Exception:
                    logger.warning("Shadow belief handler error for %s", pair, exc_info=True)

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
            tree_state = _conditional_tree_state(self._state)

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
            self._state = replace(self._state, conditional_tree_state=planned_state)

        await self.enqueue_belief(candidate.belief, observed_at=now)

    async def _maybe_run_rotation_planner(self, now: datetime) -> None:
        """Run the rotation tree planner cycle if enabled."""
        if self._rotation_planner is None or self._rotation_tree is None:
            return

        # 1. Handle expired nodes with real exchange orders
        await self._handle_rotation_expiry(now)

        # 2. Settle any queued rotation fills
        await self._settle_rotation_fills(now)

        # 3. Check fill timeouts (cancel stale entries, escalate stale exits)
        await self._check_rotation_fill_timeouts(now)

        # 4. Monitor prices for TP/SL triggers
        await self._monitor_rotation_prices(now)

        # 5. Run planner to find new candidates
        updated_tree = self._rotation_planner.plan_cycle(self._rotation_tree, now)

        # 4. Seed reference prices for newly planned nodes
        for node in updated_tree.nodes:
            if node.entry_pair and node.entry_price and node.entry_pair not in self._state.current_prices:
                await self._seed_candidate_reference_price(
                    pair=node.entry_pair,
                    reference_price=node.entry_price,
                    observed_at=now,
                )

        self._rotation_tree = updated_tree

        # 5. Execute entry orders for PLANNED child nodes
        await self._execute_rotation_entries(now)

        # 6. Persist tree state
        try:
            self._writer.save_rotation_tree(self._rotation_tree)
        except Exception:
            logger.debug("Failed to persist rotation tree")

    async def _execute_rotation_entries(self, now: datetime) -> None:
        """Place exchange orders for PLANNED child nodes (non-root)."""
        tree = self._rotation_tree
        if tree is None:
            return

        # Build set of node IDs that already have pending orders
        pending_node_ids = {
            po.rotation_node_id
            for po in self._state.bot_state.pending_orders
            if po.rotation_node_id
        }

        for node in tree.nodes:
            if node.status != RotationNodeStatus.PLANNED:
                continue
            if node.depth == 0:  # roots are not traded
                continue
            if node.node_id in pending_node_ids:
                continue
            if not node.entry_pair or node.order_side is None or not node.entry_price:
                continue

            # Skip pairs in cooldown (recently cancelled — prevents re-plan churn)
            cooldown_expiry = self._rotation_pair_cooldowns.get(node.entry_pair)
            if cooldown_expiry is not None and _time.monotonic() < cooldown_expiry:
                self._rotation_tree = cancel_planned_node(self._rotation_tree, node.node_id)
                continue

            # Compute base-asset quantity for the order
            base_qty = entry_base_quantity(node.order_side, node.quantity_total, node.entry_price)
            if base_qty <= ZERO_DECIMAL:
                continue

            client_order_id = f"kbv4-rot-{node.node_id}-entry"

            order = OrderRequest(
                pair=node.entry_pair,
                side=node.order_side,
                order_type=OrderType.LIMIT,
                quantity=base_qty,
                limit_price=node.entry_price,
                client_order_id=client_order_id,
            )
            pending = PendingOrder(
                client_order_id=client_order_id,
                kind="rotation_entry",
                pair=node.entry_pair,
                side=node.order_side,
                base_qty=base_qty,
                quote_qty=node.quantity_total if node.order_side == OrderSide.BUY else ZERO_DECIMAL,
                rotation_node_id=node.node_id,
                created_at=now,
            )

            # Try to place the order first — only add PendingOrder on success
            try:
                order_id = self._executor.execute_order(order)
            except RateLimitExceededError as exc:
                # Rate limits: skip without retry count or circuit breaker impact
                logger.info("Rotation entry rate-limited for %s: %s", node.node_id, exc)
                continue
            except InsufficientFundsError as exc:
                # Insufficient funds: cancel immediately, return capital to parent
                logger.warning("Rotation entry cancelled (insufficient funds) for %s: %s", node.node_id, exc)
                self._rotation_tree = cancel_planned_node(self._rotation_tree, node.node_id)
                self._rotation_entry_retry_counts.pop(node.node_id, None)
                # Cooldown this pair to prevent re-plan churn
                self._rotation_pair_cooldowns[node.entry_pair] = _time.monotonic() + ROTATION_PAIR_COOLDOWN_SEC
                continue
            except SafeModeBlockedError as exc:
                # Safe mode: skip without retry count (intentional block, not error)
                logger.info("Rotation entry safe-mode blocked for %s: %s", node.node_id, exc)
                continue
            except ExchangeError as exc:
                # Other exchange errors: increment retry count, cancel after max retries
                retries = self._rotation_entry_retry_counts.get(node.node_id, 0) + 1
                self._rotation_entry_retry_counts[node.node_id] = retries
                if retries >= ROTATION_ENTRY_MAX_RETRIES:
                    logger.warning(
                        "Rotation entry retries exhausted for %s (%d/%d): %s",
                        node.node_id, retries, ROTATION_ENTRY_MAX_RETRIES, exc,
                    )
                    self._rotation_tree = cancel_planned_node(self._rotation_tree, node.node_id)
                    self._rotation_entry_retry_counts.pop(node.node_id, None)
                    self._rotation_pair_cooldowns[node.entry_pair] = _time.monotonic() + ROTATION_PAIR_COOLDOWN_SEC
                else:
                    logger.warning(
                        "Rotation entry blocked for %s (retry %d/%d): %s",
                        node.node_id, retries, ROTATION_ENTRY_MAX_RETRIES, exc,
                    )
                continue

            # Order placed — clear any stale retry count and track it
            self._rotation_entry_retry_counts.pop(node.node_id, None)
            pending = replace(pending, exchange_order_id=order_id)
            async with self._state_lock:
                self._state = replace(
                    self._state,
                    bot_state=replace(
                        self._state.bot_state,
                        pending_orders=self._state.bot_state.pending_orders + (pending,),
                    ),
                )
            self._writer.upsert_order(
                order_id=order_id,
                pair=node.entry_pair,
                client_order_id=client_order_id,
                kind="rotation_entry",
                side=node.order_side.value,
                base_qty=base_qty,
                filled_qty=ZERO_DECIMAL,
                quote_qty=pending.quote_qty,
                limit_price=node.entry_price,
                exchange_order_id=order_id,
                rotation_node_id=node.node_id,
            )
            logger.info(
                "Rotation entry: %s %s qty=%s @ %s (node=%s, order=%s)",
                node.order_side.value, node.entry_pair, base_qty,
                node.entry_price, node.node_id, order_id,
            )

    async def _settle_rotation_fills(self, now: datetime) -> None:
        """Apply queued rotation fills to the tree."""
        if self._rotation_tree is None:
            return

        fills = list(self._rotation_fill_queue)
        self._rotation_fill_queue.clear()

        for node_id, fill_qty, fill_price, kind in fills:
            node = node_by_id(self._rotation_tree, node_id)
            if node is None:
                logger.warning("Rotation fill for unknown node %s", node_id)
                continue

            if kind == "rotation_entry":
                if node.order_side is None:
                    continue
                entry_cost = node.quantity_total  # Parent-denomination cost before conversion
                dest_qty = destination_quantity(node.order_side, fill_qty, fill_price)
                # Compute fee-aware TP/SL prices
                tp_pct = self._settings.rotation_take_profit_pct
                sl_pct = self._settings.rotation_stop_loss_pct
                fee_pct = self._settings.kraken_maker_fee_pct * 2  # round-trip
                if node.order_side == OrderSide.BUY:
                    tp_price = fill_price * (1 + Decimal(str((tp_pct + fee_pct) / 100)))
                    sl_price = fill_price * (1 - Decimal(str(sl_pct / 100)))
                else:  # SELL side
                    tp_price = fill_price * (1 - Decimal(str((tp_pct + fee_pct) / 100)))
                    sl_price = fill_price * (1 + Decimal(str(sl_pct / 100)))
                # Transition PLANNED → OPEN with converted quantity + TP/SL
                self._rotation_tree = update_node(
                    self._rotation_tree, node_id,
                    status=RotationNodeStatus.OPEN,
                    quantity_total=dest_qty,
                    quantity_free=dest_qty,
                    quantity_reserved=ZERO_DECIMAL,
                    entry_price=fill_price,
                    fill_price=fill_price,
                    entry_cost=entry_cost,
                    take_profit_price=tp_price,
                    stop_loss_price=sl_price,
                    trailing_stop_high=fill_price,
                    opened_at=now,
                )
                # Release parent's reserved quantity
                if node.parent_node_id:
                    parent = node_by_id(self._rotation_tree, node.parent_node_id)
                    if parent:
                        new_reserved = max(ZERO_DECIMAL, parent.quantity_reserved - node.quantity_total)
                        self._rotation_tree = update_node(
                            self._rotation_tree, parent.node_id,
                            quantity_reserved=new_reserved,
                        )
                logger.info(
                    "Rotation fill settled: node=%s OPEN, dest_qty=%s %s",
                    node_id, dest_qty, node.asset,
                )
                self._rotation_events.append(RotationEvent(
                    timestamp=now, node_id=node_id, event_type="fill_entry",
                    pair=node.entry_pair or "", details={
                        "dest_qty": str(dest_qty), "asset": node.asset,
                        "fill_price": str(fill_price),
                    },
                ))

            elif kind == "rotation_exit":
                if node.order_side is None:
                    continue
                proceeds = exit_proceeds(node.order_side, fill_qty, fill_price)
                # Mark node as CLOSED with P&L data
                self._rotation_tree = update_node(
                    self._rotation_tree, node_id,
                    status=RotationNodeStatus.CLOSED,
                    exit_price=fill_price,
                    closed_at=now,
                    exit_proceeds=proceeds,
                )
                # Return proceeds to parent
                if node.parent_node_id:
                    parent = node_by_id(self._rotation_tree, node.parent_node_id)
                    if parent:
                        self._rotation_tree = update_node(
                            self._rotation_tree, parent.node_id,
                            quantity_free=parent.quantity_free + proceeds,
                        )
                logger.info(
                    "Rotation exit settled: node=%s CLOSED, proceeds=%s → parent=%s",
                    node_id, proceeds, node.parent_node_id,
                )
                pnl = str(proceeds - node.entry_cost) if node.entry_cost else ""
                self._rotation_events.append(RotationEvent(
                    timestamp=now, node_id=node_id, event_type="fill_exit",
                    pair=node.entry_pair or "", details={
                        "proceeds": str(proceeds), "exit_price": str(fill_price),
                        "exit_reason": node.exit_reason or "", "pnl": pnl,
                    },
                ))

    async def _monitor_rotation_prices(self, now: datetime) -> None:
        """Check OPEN nodes for TP/SL triggers. Called every cycle."""
        tree = self._rotation_tree
        if tree is None:
            return

        # Build set of nodes already pending exit
        pending_exit_ids = {
            po.rotation_node_id
            for po in self._state.bot_state.pending_orders
            if po.rotation_node_id and po.kind == "rotation_exit"
        }

        for node in tree.nodes:
            if node.status != RotationNodeStatus.OPEN or node.depth == 0:
                continue
            if node.node_id in pending_exit_ids:
                continue
            if node.fill_price is None or node.order_side is None:
                continue
            if node.take_profit_price is None or node.stop_loss_price is None:
                continue

            # Get current price
            snap = self._state.current_prices.get(node.entry_pair)
            if snap is None:
                continue
            current_price = snap.price if hasattr(snap, "price") else snap

            # Update trailing stop high
            if node.order_side == OrderSide.BUY:
                if node.trailing_stop_high is None or current_price > node.trailing_stop_high:
                    self._rotation_tree = update_node(
                        self._rotation_tree, node.node_id,
                        trailing_stop_high=current_price,
                    )
            else:  # SELL: trailing low
                if node.trailing_stop_high is None or current_price < node.trailing_stop_high:
                    self._rotation_tree = update_node(
                        self._rotation_tree, node.node_id,
                        trailing_stop_high=current_price,
                    )

            # Check take-profit
            tp_hit = (
                (node.order_side == OrderSide.BUY and current_price >= node.take_profit_price)
                or (node.order_side == OrderSide.SELL and current_price <= node.take_profit_price)
            )
            if tp_hit:
                logger.info(
                    "Rotation TP hit for %s: current=%s >= tp=%s",
                    node.node_id, current_price, node.take_profit_price,
                )
                self._rotation_events.append(RotationEvent(
                    timestamp=now, node_id=node.node_id, event_type="tp_hit",
                    pair=node.entry_pair or "", details={
                        "current_price": str(current_price),
                        "tp_price": str(node.take_profit_price),
                        "fill_price": str(node.fill_price or ""),
                    },
                ))
                self._rotation_tree = update_node(
                    self._rotation_tree, node.node_id, exit_reason="take_profit",
                )
                await self._close_rotation_node(node, order_type=OrderType.LIMIT)
                continue

            # Check stop-loss
            sl_hit = (
                (node.order_side == OrderSide.BUY and current_price <= node.stop_loss_price)
                or (node.order_side == OrderSide.SELL and current_price >= node.stop_loss_price)
            )
            if sl_hit:
                logger.warning(
                    "Rotation SL hit for %s: current=%s <= sl=%s",
                    node.node_id, current_price, node.stop_loss_price,
                )
                self._rotation_events.append(RotationEvent(
                    timestamp=now, node_id=node.node_id, event_type="sl_hit",
                    pair=node.entry_pair or "", details={
                        "current_price": str(current_price),
                        "sl_price": str(node.stop_loss_price),
                        "fill_price": str(node.fill_price or ""),
                    },
                ))
                self._rotation_tree = update_node(
                    self._rotation_tree, node.node_id, exit_reason="stop_loss",
                )
                await self._close_rotation_node(node, order_type=OrderType.MARKET)
                continue

    async def _check_rotation_fill_timeouts(self, now: datetime) -> None:
        """Cancel stale entry orders, escalate stale exit orders to MARKET."""
        if self._rotation_tree is None:
            return

        timeout_exit = timedelta(minutes=self._settings.rotation_exit_fill_timeout_min)

        for po in self._state.bot_state.pending_orders:
            if not po.rotation_node_id or po.created_at is None:
                continue

            age = now - po.created_at
            node = node_by_id(self._rotation_tree, po.rotation_node_id)
            if node is None:
                continue

            # Stale entry: cancel order + cancel node
            # Dynamic entry timeout: 25% of estimated window, capped at config max
            if po.kind == "rotation_entry":
                if node.window_hours and node.window_hours > 0:
                    dynamic_minutes = min(
                        node.window_hours * 60 * 0.25,  # 25% of window
                        self._settings.rotation_entry_fill_timeout_min * 4,  # max 4x config
                    )
                    # Floor at config minimum to avoid extremely short timeouts
                    dynamic_minutes = max(dynamic_minutes, self._settings.rotation_entry_fill_timeout_min)
                else:
                    dynamic_minutes = self._settings.rotation_entry_fill_timeout_min
                node_timeout_entry = timedelta(minutes=dynamic_minutes)

                if age >= node_timeout_entry:
                    logger.warning(
                        "Rotation entry timeout for %s (age=%s, timeout=%sm): cancelling",
                        node.node_id, age, dynamic_minutes,
                    )
                    self._rotation_events.append(RotationEvent(
                        timestamp=now, node_id=node.node_id, event_type="entry_timeout",
                        pair=node.entry_pair or "", details={
                            "age_seconds": str(int(age.total_seconds())),
                        },
                    ))
                    try:
                        await self._execute_cancel_order(
                            CancelOrder(client_order_id=po.client_order_id)
                        )
                    except Exception as exc:
                        logger.warning("Failed to cancel stale entry: %s", exc)
                    # Remove pending order and cancel node
                    async with self._state_lock:
                        remaining = tuple(
                            p for p in self._state.bot_state.pending_orders
                            if p.rotation_node_id != po.rotation_node_id
                        )
                        self._state = replace(
                            self._state,
                            bot_state=replace(self._state.bot_state, pending_orders=remaining),
                        )
                    self._rotation_tree = cancel_planned_node(self._rotation_tree, node.node_id)
                    self._rotation_pair_cooldowns[node.entry_pair] = _time.monotonic() + ROTATION_PAIR_COOLDOWN_SEC

            # Stale exit: escalate to MARKET
            elif po.kind == "rotation_exit" and age >= timeout_exit:
                logger.warning(
                    "Rotation exit timeout for %s (age=%s): escalating to MARKET",
                    node.node_id, age,
                )
                self._rotation_events.append(RotationEvent(
                    timestamp=now, node_id=node.node_id, event_type="exit_escalation",
                    pair=node.entry_pair or "", details={
                        "age_seconds": str(int(age.total_seconds())),
                    },
                ))
                try:
                    await self._execute_cancel_order(
                        CancelOrder(client_order_id=po.client_order_id)
                    )
                except Exception as exc:
                    logger.warning("Failed to cancel stale exit limit: %s", exc)
                # Remove old pending exit, then resubmit as MARKET
                async with self._state_lock:
                    remaining = tuple(
                        p for p in self._state.bot_state.pending_orders
                        if p.rotation_node_id != po.rotation_node_id
                    )
                    self._state = replace(
                        self._state,
                        bot_state=replace(self._state.bot_state, pending_orders=remaining),
                    )
                await self._close_rotation_node(node, order_type=OrderType.MARKET)

    async def _handle_rotation_expiry(self, now: datetime) -> None:
        """Handle expired rotation nodes with real exchange effects."""
        if self._rotation_tree is None:
            return

        expired = expired_nodes(self._rotation_tree, now)
        if not expired:
            return

        # Sort by depth descending — close children before parents
        expired_sorted = sorted(expired, key=lambda n: n.depth, reverse=True)

        for node in expired_sorted:
            logger.info(
                "Rotation node expired: %s (%s, depth=%d, status=%s)",
                node.node_id, node.asset, node.depth, node.status,
            )

            if node.status == RotationNodeStatus.OPEN and node.depth > 0:
                # OPEN node with holdings — place exit order
                self._rotation_tree = update_node(
                    self._rotation_tree, node.node_id, exit_reason="timer",
                )
                await self._close_rotation_node(node, reason="expired", now=now)
            elif node.status == RotationNodeStatus.PLANNED and node.depth > 0:
                # PLANNED but not yet filled — cancel pending order, return reserved
                await self._cancel_rotation_entry(node)
            else:
                # Root or already closing — just mark expired
                self._rotation_tree = cascade_close(
                    self._rotation_tree, node.node_id,
                    status=RotationNodeStatus.EXPIRED,
                )

    async def _close_rotation_node(
        self, node, *, reason: str = "", now: datetime | None = None,
        order_type: OrderType = OrderType.LIMIT,
    ) -> None:
        """Place exit order for an OPEN rotation node."""
        if now is None:
            now = self._utc_now()
        if node.order_side is None or not node.entry_pair:
            self._rotation_tree = update_node(
                self._rotation_tree, node.node_id,
                status=RotationNodeStatus.EXPIRED,
            )
            return

        # Get current price for the pair
        price_snap = self._state.current_prices.get(node.entry_pair)
        current_price = price_snap.price if price_snap else node.entry_price
        if not current_price or current_price <= ZERO_DECIMAL:
            self._rotation_tree = update_node(
                self._rotation_tree, node.node_id,
                status=RotationNodeStatus.EXPIRED,
            )
            return

        # Compute exit order
        exit_side = OrderSide.SELL if node.order_side == OrderSide.BUY else OrderSide.BUY
        base_qty = exit_base_quantity(node.order_side, node.quantity_total, current_price)
        if base_qty <= ZERO_DECIMAL:
            self._rotation_tree = update_node(
                self._rotation_tree, node.node_id,
                status=RotationNodeStatus.EXPIRED,
            )
            return

        client_order_id = f"kbv4-rot-{node.node_id}-exit"
        order = OrderRequest(
            pair=node.entry_pair,
            side=exit_side,
            order_type=order_type,
            quantity=base_qty,
            limit_price=current_price if order_type == OrderType.LIMIT else None,
            client_order_id=client_order_id,
        )
        pending = PendingOrder(
            client_order_id=client_order_id,
            kind="rotation_exit",
            pair=node.entry_pair,
            side=exit_side,
            base_qty=base_qty,
            quote_qty=ZERO_DECIMAL,
            rotation_node_id=node.node_id,
            created_at=now,
        )

        # Try to place exit order first — only track on success
        try:
            order_id = self._executor.execute_order(order)
        except (ExchangeError, SafeModeBlockedError) as exc:
            logger.warning(
                "Rotation exit blocked for %s (%s): %s — will retry next cycle",
                node.node_id, reason, exc,
            )
            return  # Keep node OPEN so TP/SL can retry next cycle

        # Order placed — track PendingOrder and mark CLOSING
        pending = replace(pending, exchange_order_id=order_id)
        async with self._state_lock:
            self._state = replace(
                self._state,
                bot_state=replace(
                    self._state.bot_state,
                    pending_orders=self._state.bot_state.pending_orders + (pending,),
                ),
            )
        self._rotation_tree = update_node(
            self._rotation_tree, node.node_id,
            status=RotationNodeStatus.CLOSING,
        )
        self._writer.upsert_order(
            order_id=order_id,
            pair=node.entry_pair,
            client_order_id=client_order_id,
            kind="rotation_exit",
            side=exit_side.value,
            base_qty=base_qty,
            filled_qty=ZERO_DECIMAL,
            quote_qty=ZERO_DECIMAL,
            limit_price=current_price,
            exchange_order_id=order_id,
            rotation_node_id=node.node_id,
        )
        logger.info(
            "Rotation exit: %s %s qty=%s @ %s (node=%s, reason=%s, order=%s)",
            exit_side.value, node.entry_pair, base_qty,
            current_price, node.node_id, reason, order_id,
        )

    async def _cancel_rotation_entry(self, node) -> None:
        """Cancel pending order for a PLANNED rotation node and return reserved qty to parent."""
        # Find and cancel the pending order
        pending = next(
            (po for po in self._state.bot_state.pending_orders
             if po.rotation_node_id == node.node_id),
            None,
        )
        if pending:
            await self._execute_cancel_order(CancelOrder(client_order_id=pending.client_order_id))
            # Remove PendingOrder from state
            async with self._state_lock:
                remaining = tuple(
                    po for po in self._state.bot_state.pending_orders
                    if po.rotation_node_id != node.node_id
                )
                self._state = replace(
                    self._state,
                    bot_state=replace(self._state.bot_state, pending_orders=remaining),
                )

        # Return reserved quantity to parent and mark CANCELLED
        self._rotation_tree = cancel_planned_node(self._rotation_tree, node.node_id)
        self._rotation_entry_retry_counts.pop(node.node_id, None)

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
                "health": {
                    "uptime_seconds": int((datetime.now(timezone.utc) - self._runtime_started_at).total_seconds()),
                    "version": "0.1.0",
                },
                "portfolio": jsonable_encoder(dashboard_state.portfolio),
                "positions": {"positions": jsonable_encoder(dashboard_state.positions)},
                "grid": jsonable_encoder(dashboard_state.grids),
                "beliefs": jsonable_encoder(dashboard_state.beliefs),
                "stats": jsonable_encoder(dashboard_state.stats),
                "reconciliation": jsonable_encoder(dashboard_state.reconciliation),
                "rotation_tree": jsonable_encoder(dashboard_state.rotation_tree),
                "pending_orders": jsonable_encoder(dashboard_state.pending_orders),
                "rotation_events": [
                    {"timestamp": e.timestamp.isoformat(), "node_id": e.node_id,
                     "event_type": e.event_type, "pair": e.pair, "details": e.details}
                    for e in self._rotation_events
                ],
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
        # Compute rotation tree total value in USD
        tree_value = _compute_rotation_tree_value(
            self._rotation_tree, state.current_prices,
        )
        # Merge trading beliefs with display-only beliefs (low-confidence)
        all_beliefs = _build_belief_entries(
            state.bot_state.beliefs,
            display_beliefs=self._display_beliefs,
            confidence_gate=self._settings.min_belief_confidence,
        )
        return DashboardState(
            portfolio=state.bot_state.portfolio,
            positions=_build_position_snapshots(
                state.bot_state.portfolio.positions,
                state.current_prices,
            ),
            grids=_build_grid_snapshots(state.bot_state.portfolio.positions),
            beliefs=all_beliefs,
            stats=StrategyStatsSnapshot(),
            reconciliation=ReconciliationSnapshot(
                checked_at=state.last_reconcile_at,
                report=state.last_reconciliation_report,
            ),
            rotation_tree=_build_rotation_tree_snapshot(
                self._rotation_tree, tree_value_usd=tree_value,
            ),
            pending_orders=tuple(
                {"client_order_id": po.client_order_id, "pair": po.pair, "side": po.side.value if hasattr(po.side, "value") else str(po.side or ""), "kind": po.kind, "base_qty": str(po.base_qty), "rotation_node_id": po.rotation_node_id or ""}
                for po in state.bot_state.pending_orders
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


def _conditional_tree_state(state: SchedulerState) -> ConditionalTreeState:
    tree_state = state.conditional_tree_state
    if isinstance(tree_state, ConditionalTreeState):
        return tree_state
    return ConditionalTreeState()


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
    *,
    display_beliefs: dict[str, BeliefSnapshot] | None = None,
    confidence_gate: float = 0.0,
) -> tuple[BeliefEntry, ...]:
    entries: list[BeliefEntry] = []
    seen_pairs: set[str] = set()
    # Active (trading) beliefs first
    for belief in beliefs:
        seen_pairs.add(belief.pair)
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
    # Display-only beliefs (below confidence gate) — not used for trading
    if display_beliefs:
        for pair, belief in display_beliefs.items():
            if pair in seen_pairs:
                continue
            if belief.confidence >= confidence_gate:
                continue  # already in trading beliefs
            for source in belief.sources:
                entries.append(
                    BeliefEntry(
                        pair=belief.pair,
                        source=source if isinstance(source, BeliefSource) else BeliefSource(str(source)),
                        direction=belief.direction,
                        confidence=belief.confidence,
                        regime=belief.regime,
                        filtered=True,
                    )
                )
    return tuple(sorted(entries, key=lambda item: (item.pair, item.source.value)))


def _apply_exit_offset(
    price: Decimal,
    position_side: PositionSide,
    offset_pct: float,
) -> Decimal:
    """Apply a marketable-limit offset to improve exit fill probability.

    Long exits (sells) go slightly below trigger; short exits (buys) go
    slightly above. Result is quantized to at least the input precision
    or 4 decimal places, whichever is finer.
    """
    if price <= 0 or offset_pct <= 0:
        return price
    if position_side == PositionSide.LONG:
        multiplier = Decimal(str(1 - offset_pct / 100))
    else:
        multiplier = Decimal(str(1 + offset_pct / 100))
    raw = price * multiplier
    # Use the finer of input precision or 4 decimal places
    input_exp = price.as_tuple().exponent
    min_exp = -4
    quant_exp = min(input_exp, min_exp)  # type: ignore[arg-type]
    template = Decimal(10) ** quant_exp
    return raw.quantize(template)


_price_cache: dict[str, tuple[float, Decimal]] = {}  # pair → (monotonic_expiry, price)
_PRICE_CACHE_TTL = 300  # 5 minutes


def _compute_rotation_tree_value(
    tree: RotationTreeState | None,
    current_prices: dict,
) -> str:
    """Sum all rotation tree root assets in USD. Returns string, '~X' (partial), or 'N/A'."""
    if tree is None:
        return "0"
    total = ZERO_DECIMAL
    has_missing = False
    now = _time.monotonic()
    for node in tree.nodes:
        if node.depth != 0:
            continue
        asset = node.asset
        if asset in ("USD", "USDC"):
            total += node.quantity_total
            continue
        # Look up ASSET/USD price from current_prices (WebSocket)
        pair_key = f"{asset}/USD"
        snap = current_prices.get(pair_key)
        if snap is not None:
            price = snap.price if hasattr(snap, "price") else snap
            total += node.quantity_total * price
            continue
        # Check cache before REST fallback
        cached = _price_cache.get(pair_key)
        if cached is not None and now < cached[0]:
            total += node.quantity_total * cached[1]
            continue
        # REST fallback (cached for 5 min to avoid blocking every cycle)
        try:
            from exchange.ohlcv import fetch_ohlcv
            bars = fetch_ohlcv(pair_key, interval=60, count=1)
            if not bars.empty:
                price = Decimal(str(float(bars["close"].iloc[-1])))
                _price_cache[pair_key] = (now + _PRICE_CACHE_TTL, price)
                total += node.quantity_total * price
            else:
                has_missing = True
        except Exception:
            has_missing = True
    if has_missing:
        if total == ZERO_DECIMAL:
            return "N/A"
        return f"~{round(total, 2)}"  # Prefix ~ to signal incomplete valuation
    return str(round(total, 2))


def _build_rotation_tree_snapshot(
    tree: RotationTreeState | None,
    *,
    tree_value_usd: str = "0",
) -> RotationTreeSnapshot:
    if tree is None:
        return RotationTreeSnapshot()

    total_deployed = ZERO_DECIMAL
    total_realized_pnl = ZERO_DECIMAL
    open_count = 0
    closed_count = 0

    node_snaps_list: list[RotationNodeSnapshot] = []
    for n in tree.nodes:
        # Compute realized P&L for closed nodes (proceeds vs entry cost in parent denomination)
        realized_pnl = None
        if n.status == RotationNodeStatus.CLOSED and n.exit_proceeds is not None and n.entry_cost is not None:
            pnl = n.exit_proceeds - n.entry_cost
            realized_pnl = str(pnl)
            total_realized_pnl += pnl
            closed_count += 1
        if n.status in (RotationNodeStatus.OPEN, RotationNodeStatus.CLOSING) and n.depth > 0:
            total_deployed += n.entry_cost if n.entry_cost is not None else ZERO_DECIMAL
            open_count += 1

        node_snaps_list.append(RotationNodeSnapshot(
            node_id=n.node_id,
            parent_node_id=n.parent_node_id,
            depth=n.depth,
            asset=n.asset,
            quantity_total=str(n.quantity_total),
            quantity_free=str(n.quantity_free),
            quantity_reserved=str(n.quantity_reserved),
            status=n.status.value,
            entry_pair=n.entry_pair,
            from_asset=n.from_asset,
            order_side=n.order_side.value if n.order_side else None,
            entry_price=str(n.entry_price) if n.entry_price else None,
            confidence=n.confidence,
            deadline_at=n.deadline_at.isoformat() if n.deadline_at else None,
            opened_at=n.opened_at.isoformat() if n.opened_at else None,
            window_hours=n.window_hours,
            fill_price=str(n.fill_price) if n.fill_price else None,
            exit_price=str(n.exit_price) if n.exit_price else None,
            closed_at=n.closed_at.isoformat() if n.closed_at else None,
            exit_proceeds=str(n.exit_proceeds) if n.exit_proceeds else None,
            realized_pnl=realized_pnl,
        ))

    return RotationTreeSnapshot(
        nodes=tuple(node_snaps_list),
        root_node_ids=tree.root_node_ids,
        max_depth=tree.max_depth,
        last_planned_at=tree.last_planned_at.isoformat() if tree.last_planned_at else None,
        total_deployed=str(total_deployed),
        total_realized_pnl=str(total_realized_pnl),
        open_count=open_count,
        closed_count=closed_count,
        rotation_tree_value_usd=tree_value_usd,
        total_portfolio_value_usd=tree_value_usd,
    )


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


def _collect_root_prices(
    current_prices: dict[str, PriceSnapshot] | object,
    balances: dict[str, Decimal],
) -> dict[str, Decimal]:
    """Build asset→USD price map for rotation tree root initialization.

    Uses WebSocket prices first, falls back to OHLCV REST for missing assets.
    """
    prices: dict[str, Decimal] = {"USD": Decimal("1")}
    # Extract known prices from current_prices dict
    if isinstance(current_prices, dict):
        for pair_key, snap in current_prices.items():
            if "/" in pair_key:
                base = pair_key.split("/")[0]
                price = snap.price if isinstance(snap, PriceSnapshot) else getattr(snap, "price", None)
                if price and price > ZERO_DECIMAL:
                    prices[base] = price

    # Fetch missing prices via REST OHLCV
    from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv

    for asset in balances:
        if asset in prices:
            continue
        pair = f"{asset}/USD"
        try:
            bars = fetch_ohlcv(pair, interval=60, count=1, timeout=10.0)
            if not bars.empty:
                import pandas as pd
                close_val = pd.to_numeric(bars["close"], errors="coerce").iloc[-1]
                if close_val and close_val > 0:
                    prices[asset] = Decimal(str(close_val))
                    logger.info("Fetched REST price for %s: %s", asset, prices[asset])
        except (OHLCVFetchError, Exception) as exc:
            logger.warning("Could not fetch price for %s, skipping root: %s", asset, exc)

    return prices


__all__ = [
    "DashboardStateStore",
    "DEFAULT_CYCLE_INTERVAL_SEC",
    "DEFAULT_GUARDIAN_INTERVAL_SEC",
    "SchedulerRuntime",
    "build_initial_scheduler_state",
    "build_runtime_app",
]
