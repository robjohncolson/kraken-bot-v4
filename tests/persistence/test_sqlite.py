from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from persistence.sqlite import (
    SqliteReader,
    ensure_schema,
    open_database,
)
from trading.reconciler import RecordedPosition, RecordedState


def _memory_db() -> sqlite3.Connection:
    """Open an in-memory SQLite with the bot schema."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def _insert_position(conn: sqlite3.Connection, position_id: str, pair: str, *, closed: bool = False) -> None:
    closed_at = "2024-01-01T00:00:00" if closed else None
    conn.execute(
        "INSERT INTO positions (position_id, pair, closed_at) VALUES (?, ?, ?)",
        (position_id, pair, closed_at),
    )
    conn.commit()


def _insert_order(
    conn: sqlite3.Connection,
    order_id: str,
    pair: str,
    *,
    position_id: str | None = None,
    exchange_order_id: str | None = None,
    client_order_id: str | None = None,
    recorded_fee: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO orders (order_id, pair, position_id, exchange_order_id, "
        "client_order_id, recorded_fee) VALUES (?, ?, ?, ?, ?, ?)",
        (order_id, pair, position_id, exchange_order_id, client_order_id, recorded_fee),
    )
    conn.commit()


# ── Schema tests ────────────────────────────────────────────


def test_ensure_schema_creates_tables() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "positions" in tables
    assert "orders" in tables


def test_ensure_schema_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    ensure_schema(conn)  # should not raise


# ── open_database tests ─────────────────────────────────────


def test_open_database_creates_file(tmp_path: Path) -> None:
    db_path = tmp_path / "sub" / "bot.db"
    conn = open_database(db_path)
    assert db_path.exists()
    conn.close()


def test_open_database_sets_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "bot.db"
    conn = open_database(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


# ── fetch_positions tests ───────────────────────────────────


def test_fetch_positions_empty() -> None:
    conn = _memory_db()
    reader = SqliteReader(conn)
    assert reader.fetch_positions() == ()


def test_fetch_positions_returns_open_only() -> None:
    conn = _memory_db()
    _insert_position(conn, "pos-open", "DOGE/USD")
    _insert_position(conn, "pos-closed", "BTC/USD", closed=True)

    reader = SqliteReader(conn)
    positions = reader.fetch_positions()

    assert len(positions) == 1
    assert positions[0].position_id == "pos-open"
    assert positions[0].pair == "DOGE/USD"


def test_fetch_positions_sorted_by_id() -> None:
    conn = _memory_db()
    _insert_position(conn, "pos-z", "ETH/USD")
    _insert_position(conn, "pos-a", "BTC/USD")

    reader = SqliteReader(conn)
    positions = reader.fetch_positions()

    assert positions[0].position_id == "pos-a"
    assert positions[1].position_id == "pos-z"


def test_fetch_positions_returns_recorded_position_type() -> None:
    conn = _memory_db()
    _insert_position(conn, "pos-1", "DOGE/USD")

    reader = SqliteReader(conn)
    pos = reader.fetch_positions()[0]

    assert isinstance(pos, RecordedPosition)


# ── fetch_orders tests ──────────────────────────────────────


def test_fetch_orders_empty() -> None:
    conn = _memory_db()
    reader = SqliteReader(conn)
    assert reader.fetch_orders() == ()


def test_fetch_orders_with_all_fields() -> None:
    conn = _memory_db()
    _insert_position(conn, "pos-1", "DOGE/USD")
    _insert_order(
        conn,
        "ord-1",
        "DOGE/USD",
        position_id="pos-1",
        exchange_order_id="O3KQ7H-AAAA",
        client_order_id="kbv4-dogeusd-000001",
        recorded_fee="0.10",
    )

    reader = SqliteReader(conn)
    orders = reader.fetch_orders()

    assert len(orders) == 1
    assert orders[0].order_id == "ord-1"
    assert orders[0].pair == "DOGE/USD"
    assert orders[0].position_id == "pos-1"
    assert orders[0].exchange_order_id == "O3KQ7H-AAAA"
    assert orders[0].client_order_id == "kbv4-dogeusd-000001"
    assert orders[0].recorded_fee == Decimal("0.10")


def test_fetch_orders_null_fee_is_none() -> None:
    conn = _memory_db()
    _insert_order(conn, "ord-1", "DOGE/USD")

    reader = SqliteReader(conn)
    order = reader.fetch_orders()[0]

    assert order.recorded_fee is None
    assert order.position_id is None


def test_fetch_orders_fee_as_decimal_not_float() -> None:
    conn = _memory_db()
    _insert_order(conn, "ord-1", "DOGE/USD", recorded_fee="0.123456789")

    reader = SqliteReader(conn)
    order = reader.fetch_orders()[0]

    assert order.recorded_fee == Decimal("0.123456789")
    assert isinstance(order.recorded_fee, Decimal)


def test_fetch_orders_sorted_by_id() -> None:
    conn = _memory_db()
    _insert_order(conn, "ord-z", "ETH/USD")
    _insert_order(conn, "ord-a", "BTC/USD")

    reader = SqliteReader(conn)
    orders = reader.fetch_orders()

    assert orders[0].order_id == "ord-a"
    assert orders[1].order_id == "ord-z"


# ── fetch_recorded_state tests ──────────────────────────────


def test_fetch_recorded_state_assembles_both() -> None:
    conn = _memory_db()
    _insert_position(conn, "pos-1", "DOGE/USD")
    _insert_order(conn, "ord-1", "DOGE/USD", position_id="pos-1")

    reader = SqliteReader(conn)
    state = reader.fetch_recorded_state()

    assert isinstance(state, RecordedState)
    assert len(state.positions) == 1
    assert len(state.orders) == 1


def test_fetch_recorded_state_empty_db() -> None:
    conn = _memory_db()
    reader = SqliteReader(conn)
    state = reader.fetch_recorded_state()

    assert state.positions == ()
    assert state.orders == ()
