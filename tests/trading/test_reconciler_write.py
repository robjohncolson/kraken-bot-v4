from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal

from core.types import Balance
from persistence.sqlite import SqliteReader, SqliteWriter, ensure_schema
from trading.reconciler import KrakenOrder, KrakenState, KrakenTrade, reconcile

AS_OF = datetime(2026, 3, 25, 12, 0, 0)


class RecordingSqliteWriter(SqliteWriter):
    def __init__(self, conn: sqlite3.Connection) -> None:
        super().__init__(conn)
        self.position_calls: list[tuple[str, str]] = []
        self.order_calls: list[tuple[str, str, str, str | None, str | None]] = []

    def insert_position(self, position_id: str, pair: str) -> None:
        self.position_calls.append((position_id, pair))
        super().insert_position(position_id, pair)

    def insert_order(
        self,
        order_id: str,
        pair: str,
        client_order_id: str,
        *,
        position_id: str | None = None,
        exchange_order_id: str | None = None,
        recorded_fee: Decimal | None = None,
    ) -> None:
        self.order_calls.append(
            (order_id, pair, client_order_id, position_id, exchange_order_id)
        )
        super().insert_order(
            order_id,
            pair,
            client_order_id,
            position_id=position_id,
            exchange_order_id=exchange_order_id,
            recorded_fee=recorded_fee,
        )


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _order(
    order_id: str,
    *,
    pair: str,
    client_order_id: str | None,
    opened_minutes_ago: int,
) -> KrakenOrder:
    return KrakenOrder(
        order_id=order_id,
        pair=pair,
        client_order_id=client_order_id,
        opened_at=AS_OF - timedelta(minutes=opened_minutes_ago),
    )


def _trade(
    trade_id: str,
    *,
    pair: str,
    order_id: str,
    client_order_id: str,
    position_id: str,
    filled_minutes_ago: int,
) -> KrakenTrade:
    return KrakenTrade(
        trade_id=trade_id,
        pair=pair,
        order_id=order_id,
        client_order_id=client_order_id,
        position_id=position_id,
        fee=Decimal("0.05"),
        filled_at=AS_OF - timedelta(minutes=filled_minutes_ago),
    )


def test_reconcile_seeds_only_missing_tracked_kraken_records_to_sqlite() -> None:
    conn = _memory_db()
    seed_writer = SqliteWriter(conn)
    seed_writer.insert_position("pos-existing", "BTC/USD")
    seed_writer.insert_order(
        "sb-existing",
        "BTC/USD",
        "kbv4-btcusd-000001",
        position_id="pos-existing",
        exchange_order_id="tracked-existing",
    )
    recorded_state = SqliteReader(conn).fetch_recorded_state()
    writer = RecordingSqliteWriter(conn)

    report = reconcile(
        KrakenState(
            balances=(
                Balance(asset="DOGE", available=Decimal("150")),
                Balance(asset="USD", available=Decimal("1000")),
            ),
            open_orders=(
                _order(
                    "foreign-new",
                    pair="ETH/USD",
                    client_order_id="manual-eth-1",
                    opened_minutes_ago=5,
                ),
                _order(
                    "tracked-existing",
                    pair="BTC/USD",
                    client_order_id="kbv4-btcusd-000001",
                    opened_minutes_ago=10,
                ),
                _order(
                    "tracked-new",
                    pair="DOGE/USD",
                    client_order_id="kbv4-dogeusd-000002",
                    opened_minutes_ago=3,
                ),
            ),
            trade_history=(
                _trade(
                    "trade-new",
                    pair="DOGE/USD",
                    order_id="tracked-new",
                    client_order_id="kbv4-dogeusd-000002",
                    position_id="pos-new",
                    filled_minutes_ago=1,
                ),
            ),
        ),
        recorded_state,
        sqlite_writer=writer,
        as_of=AS_OF,
    )

    assert writer.position_calls == [("pos-new", "DOGE/USD")]
    assert writer.order_calls == [
        (
            "tracked-new",
            "DOGE/USD",
            "kbv4-dogeusd-000002",
            "pos-new",
            "tracked-new",
        )
    ]

    positions = conn.execute(
        "SELECT position_id, pair FROM positions ORDER BY position_id"
    ).fetchall()
    orders = conn.execute(
        "SELECT order_id, pair, position_id, exchange_order_id, client_order_id "
        "FROM orders ORDER BY order_id"
    ).fetchall()

    assert [(row["position_id"], row["pair"]) for row in positions] == [
        ("pos-existing", "BTC/USD"),
        ("pos-new", "DOGE/USD"),
    ]
    assert [(row["order_id"], row["client_order_id"]) for row in orders] == [
        ("sb-existing", "kbv4-btcusd-000001"),
        ("tracked-new", "kbv4-dogeusd-000002"),
    ]
    assert orders[1]["position_id"] == "pos-new"
    assert orders[1]["exchange_order_id"] == "tracked-new"
    assert report.foreign_orders[0].order_id == "foreign-new"
    assert report.untracked_assets == ()
