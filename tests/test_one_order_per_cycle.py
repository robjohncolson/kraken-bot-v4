"""Tests for one-order-per-cycle rotation entry behaviour."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

from core.config import load_settings
from core.errors import InsufficientFundsError
from core.types import (
    Balance,
    BotState,
    OrderSide,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
)
from exchange.models import KrakenState
from exchange.websocket import ConnectionState
from persistence.sqlite import ensure_schema
from runtime_loop import SchedulerRuntime
from scheduler import SchedulerConfig, SchedulerState
from trading.reconciler import RecordedState

NOW = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

def _settings():
    return load_settings({
        "KRAKEN_API_KEY": "key",
        "KRAKEN_API_SECRET": "secret",
        "WEB_PORT": "8081",
        "ENABLE_ROTATION_TREE": "false",
    })


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


class FakeExecutor:
    """Minimal executor that records placed orders."""

    def __init__(self, *, fail_pairs: set[str] | None = None):
        self.orders: list[object] = []
        self.fail_pairs = fail_pairs or set()

    def fetch_kraken_state(self) -> KrakenState:
        return KrakenState(
            balances=(Balance(asset="USD", available=Decimal("10000")),),
        )

    def get_ws_token(self) -> str:
        return "ws-token"

    def execute_order(self, order):
        if order.pair in self.fail_pairs:
            raise InsufficientFundsError(f"Insufficient funds for {order.pair}")
        self.orders.append(order)
        return f"order-{len(self.orders)}"


class FakeWebSocket:
    def __init__(self, on_tick, on_fill):
        self.state = ConnectionState.CONNECTED
        self.ticker_subscriptions: list[tuple[str, ...]] = []
        self.execution_tokens: list[str] = []

    async def connect(self):
        self.state = ConnectionState.CONNECTED

    async def disconnect(self):
        self.state = ConnectionState.DISCONNECTED

    async def subscribe_ticker(self, pairs):
        self.ticker_subscriptions.append(tuple(pairs))

    async def subscribe_executions(self, token: str):
        self.execution_tokens.append(token)


def _make_runtime(executor: FakeExecutor, tree: RotationTreeState) -> SchedulerRuntime:
    """Build a SchedulerRuntime with a pre-set rotation tree."""
    runtime = SchedulerRuntime(
        settings=_settings(),
        executor=executor,
        conn=_memory_db(),
        initial_state=SchedulerState(
            bot_state=BotState(),
            kraken_state=KrakenState(
                balances=(Balance(asset="USD", available=Decimal("10000")),),
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
        websocket_factory=lambda on_tick, on_fill: FakeWebSocket(on_tick, on_fill),
        serve_dashboard=False,
        sse_publisher=lambda *a, **kw: None,
        heartbeat_writer=lambda snapshot: None,
        utc_now=lambda: NOW,
    )
    # Inject rotation tree directly (bypasses planner initialisation)
    runtime._rotation_tree = tree
    return runtime


def _planned_node(
    node_id: str,
    pair: str,
    confidence: float,
    *,
    side: OrderSide = OrderSide.BUY,
    entry_price: Decimal = Decimal("100"),
    quantity: Decimal = Decimal("10"),
    parent_node_id: str = "root-1",
) -> RotationNode:
    return RotationNode(
        node_id=node_id,
        parent_node_id=parent_node_id,
        depth=1,
        asset=pair.split("/")[0],
        quantity_total=quantity,
        quantity_free=quantity,
        entry_pair=pair,
        from_asset="USD",
        order_side=side,
        entry_price=entry_price,
        confidence=confidence,
        status=RotationNodeStatus.PLANNED,
    )


ROOT = RotationNode(
    node_id="root-1",
    parent_node_id=None,
    depth=0,
    asset="USD",
    quantity_total=Decimal("10000"),
    quantity_free=Decimal("9000"),
    quantity_reserved=Decimal("1000"),
    status=RotationNodeStatus.OPEN,
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_only_one_order_per_cycle():
    """When multiple PLANNED nodes exist, only one order is placed."""
    async def scenario():
        node_a = _planned_node("a", "BTC/USD", confidence=0.8)
        node_b = _planned_node("b", "ETH/USD", confidence=0.7)
        tree = RotationTreeState(
            nodes=(ROOT, node_a, node_b),
            root_node_ids=("root-1",),
        )
        executor = FakeExecutor()
        runtime = _make_runtime(executor, tree)

        await runtime._execute_rotation_entries(NOW)

        assert len(executor.orders) == 1, (
            f"Expected exactly 1 order, got {len(executor.orders)}"
        )

    asyncio.run(scenario())


def test_highest_confidence_selected_first():
    """The node with the highest confidence is the one that gets an order."""
    async def scenario():
        node_low = _planned_node("low", "ETH/USD", confidence=0.5)
        node_high = _planned_node("high", "BTC/USD", confidence=0.9)
        tree = RotationTreeState(
            nodes=(ROOT, node_low, node_high),
            root_node_ids=("root-1",),
        )
        executor = FakeExecutor()
        runtime = _make_runtime(executor, tree)

        await runtime._execute_rotation_entries(NOW)

        assert len(executor.orders) == 1
        assert executor.orders[0].pair == "BTC/USD"

    asyncio.run(scenario())


def test_fallback_on_preflight_failure():
    """If the highest-confidence node fails, the next node is tried."""
    async def scenario():
        node_a = _planned_node("a", "BTC/USD", confidence=0.9)
        node_b = _planned_node("b", "ETH/USD", confidence=0.7)
        tree = RotationTreeState(
            nodes=(ROOT, node_a, node_b),
            root_node_ids=("root-1",),
        )
        # BTC/USD will raise InsufficientFundsError
        executor = FakeExecutor(fail_pairs={"BTC/USD"})
        runtime = _make_runtime(executor, tree)

        await runtime._execute_rotation_entries(NOW)

        # a failed, b should succeed
        assert len(executor.orders) == 1
        assert executor.orders[0].pair == "ETH/USD"

    asyncio.run(scenario())


def test_exit_orders_unaffected():
    """Exit orders use _close_rotation_node, not _execute_rotation_entries.

    Verify that an OPEN node is NOT touched by _execute_rotation_entries —
    exits go through a completely separate code path.
    """
    async def scenario():
        open_node = RotationNode(
            node_id="open-1",
            parent_node_id="root-1",
            depth=1,
            asset="BTC",
            quantity_total=Decimal("1"),
            quantity_free=Decimal("1"),
            entry_pair="BTC/USD",
            from_asset="USD",
            order_side=OrderSide.BUY,
            entry_price=Decimal("100"),
            confidence=0.99,
            status=RotationNodeStatus.OPEN,
        )
        planned_node = _planned_node("p1", "ETH/USD", confidence=0.7)
        tree = RotationTreeState(
            nodes=(ROOT, open_node, planned_node),
            root_node_ids=("root-1",),
        )
        executor = FakeExecutor()
        runtime = _make_runtime(executor, tree)

        await runtime._execute_rotation_entries(NOW)

        # Only the PLANNED node should have an order; the OPEN node is skipped
        assert len(executor.orders) == 1
        assert executor.orders[0].pair == "ETH/USD"

    asyncio.run(scenario())
