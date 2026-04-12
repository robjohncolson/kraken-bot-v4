import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.config import Settings, load_settings
from core.types import OrderSide, RotationNode, RotationNodeStatus, RotationTreeState
from exchange.models import KrakenState
from exchange.websocket import ConnectionState
from persistence.sqlite import ensure_schema
from runtime_loop import SchedulerRuntime, build_initial_scheduler_state
from trading.reconciler import RecordedState, ReconciliationReport

NOW = datetime(2026, 4, 6, 1, 25, 7, tzinfo=timezone.utc)
FILL_QTY = Decimal("21.11138898")
LEGACY_ROOT_USD_VALUE = Decimal("36.9612")


class FakeExecutor:
    def __init__(self) -> None:
        self._client = object()
        self.kraken_state = KrakenState()

    def fetch_kraken_state(self) -> KrakenState:
        return self.kraken_state

    def fetch_open_orders(self) -> tuple:
        return ()

    def fetch_trade_history(self) -> tuple:
        return ()

    def get_ws_token(self) -> str:
        return "ws-token"

    def execute_order(self, _order) -> str:
        return "order-1"


class FakeRuntimeWebSocket:
    def __init__(self, _on_tick, _on_fill) -> None:
        self.state = ConnectionState.DISCONNECTED

    async def connect(self) -> None:
        self.state = ConnectionState.CONNECTED

    async def disconnect(self) -> None:
        self.state = ConnectionState.DISCONNECTED

    async def subscribe_ticker(self, _pairs) -> None:
        return None

    async def subscribe_executions(self, _token: str) -> None:
        return None


async def _noop_publish(*, event: str, data, event_id: str | None = None) -> None:
    del event, data, event_id


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


def _runtime() -> SchedulerRuntime:
    return SchedulerRuntime(
        settings=_settings(),
        executor=FakeExecutor(),
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


def _quote_side_root(pair: str, price: Decimal) -> RotationNode:
    return RotationNode(
        node_id="root-usd",
        parent_node_id=None,
        depth=0,
        asset="USD",
        quantity_total=LEGACY_ROOT_USD_VALUE,
        quantity_free=Decimal("0"),
        entry_pair=pair,
        order_side=OrderSide.SELL,
        entry_price=price,
        fill_price=price,
        entry_cost=LEGACY_ROOT_USD_VALUE,
        opened_at=NOW - timedelta(hours=2),
        confidence=0.72,
        exit_reason="root_exit_bearish",
        status=RotationNodeStatus.CLOSING,
    )


def _settle_quote_side_root_exit(pair: str, price: Decimal) -> sqlite3.Row:
    async def scenario() -> sqlite3.Row:
        runtime = _runtime()
        root = _quote_side_root(pair, price)
        runtime._rotation_tree = RotationTreeState(
            nodes=(root,),
            root_node_ids=(root.node_id,),
        )
        runtime._rotation_fill_queue.append(
            (
                root.node_id,
                FILL_QTY,
                price,
                "rotation_exit",
                Decimal("0"),
            )
        )

        await runtime._settle_rotation_fills(NOW)

        outcomes = runtime._reader.fetch_trade_outcomes(lookback_days=3650)
        assert len(outcomes) == 1
        return outcomes[0]

    return asyncio.run(scenario())


def test_root_quote_side_exit_fill_uses_executed_quote_notional() -> None:
    outcome = _settle_quote_side_root_exit("USDT/USD", Decimal("0.99965"))
    expected_notional = FILL_QTY * Decimal("0.99965")

    assert Decimal(outcome["entry_cost"]) == expected_notional
    assert Decimal(outcome["exit_proceeds"]) == expected_notional
    assert abs(Decimal(outcome["net_pnl"])) < Decimal("0.10")
    assert outcome["anomaly_flag"] is None


@pytest.mark.parametrize(
    ("pair", "price"),
    (
        ("USDT/USD", Decimal("0.99965")),
        ("USDC/USD", Decimal("1.00010")),
        ("DAI/USD", Decimal("1.00000")),
        ("PYUSD/USD", Decimal("0.99990")),
    ),
)
def test_root_stablecoin_parity_guard_keeps_pnl_under_ten_percent(
    pair: str,
    price: Decimal,
) -> None:
    outcome = _settle_quote_side_root_exit(pair, price)
    entry_cost = Decimal(outcome["entry_cost"])
    net_pnl = Decimal(outcome["net_pnl"])

    assert entry_cost > Decimal("0")
    assert abs(net_pnl) < entry_cost * Decimal("0.10")
    assert outcome["anomaly_flag"] is None


def test_ensure_schema_flags_historical_root_stablecoin_outlier() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE trade_outcomes (
            id INTEGER PRIMARY KEY,
            node_id TEXT NOT NULL,
            pair TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price TEXT NOT NULL,
            exit_price TEXT NOT NULL,
            entry_cost TEXT NOT NULL,
            exit_proceeds TEXT NOT NULL,
            net_pnl TEXT NOT NULL,
            fee_total TEXT,
            exit_reason TEXT NOT NULL,
            hold_hours REAL,
            confidence REAL,
            opened_at TEXT NOT NULL,
            closed_at TEXT NOT NULL,
            node_depth INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute(
        "INSERT INTO trade_outcomes ("
        "node_id, pair, direction, entry_price, exit_price, entry_cost, "
        "exit_proceeds, net_pnl, fee_total, exit_reason, hold_hours, confidence, "
        "opened_at, closed_at, node_depth"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "root-usd",
            "USDT/USD",
            "sell",
            "0.99965",
            "0.99965",
            "36.9612",
            "21.11138898",
            "-15.84981102",
            "0",
            "root_exit_bearish",
            1.0,
            0.7,
            "2026-04-06T01:00:00+00:00",
            "2026-04-06T01:25:07+00:00",
            0,
        ),
    )
    conn.commit()

    ensure_schema(conn)

    row = conn.execute(
        "SELECT anomaly_flag FROM trade_outcomes WHERE id = 1"
    ).fetchone()
    assert row is not None
    assert row["anomaly_flag"] == "root_stablecoin_parity_outlier"
