from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from persistence.sqlite import SqliteWriteError, SqliteWriter, ensure_schema


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def test_insert_order_creates_row_with_position_id() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    writer.insert_position("pos-1", "DOGE/USD")

    writer.insert_order(
        "ord-1",
        "DOGE/USD",
        "kbv4-dogeusd-000001",
        position_id="pos-1",
        exchange_order_id="O3KQ7H-AAAA",
        recorded_fee=Decimal("0.10"),
    )

    row = conn.execute(
        "SELECT order_id, pair, position_id, exchange_order_id, client_order_id, recorded_fee "
        "FROM orders WHERE order_id = ?",
        ("ord-1",),
    ).fetchone()
    assert row is not None
    assert row["order_id"] == "ord-1"
    assert row["pair"] == "DOGE/USD"
    assert row["position_id"] == "pos-1"
    assert row["exchange_order_id"] == "O3KQ7H-AAAA"
    assert row["client_order_id"] == "kbv4-dogeusd-000001"
    assert row["recorded_fee"] == "0.10"


def test_insert_order_without_position_id_leaves_foreign_key_null() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    writer.insert_order("ord-1", "DOGE/USD", "kbv4-dogeusd-000001")

    row = conn.execute(
        "SELECT position_id, exchange_order_id, recorded_fee FROM orders WHERE order_id = ?",
        ("ord-1",),
    ).fetchone()
    assert row is not None
    assert row["position_id"] is None
    assert row["exchange_order_id"] is None
    assert row["recorded_fee"] is None


def test_insert_order_duplicate_is_ignored() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    writer.insert_order("ord-1", "DOGE/USD", "kbv4-dogeusd-000001")
    writer.insert_order("ord-1", "BTC/USD", "kbv4-btcusd-000001")

    rows = conn.execute(
        "SELECT order_id, pair, client_order_id FROM orders WHERE order_id = ?",
        ("ord-1",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["pair"] == "DOGE/USD"
    assert rows[0]["client_order_id"] == "kbv4-dogeusd-000001"


def test_insert_order_missing_position_enforces_foreign_key() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    with pytest.raises(SqliteWriteError):
        writer.insert_order(
            "ord-1",
            "DOGE/USD",
            "kbv4-dogeusd-000001",
            position_id="missing-pos",
        )

    count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert count == 0


def test_ensure_schema_creates_ledger_table_with_expected_columns() -> None:
    conn = _memory_db()

    columns = conn.execute("PRAGMA table_info(ledger)").fetchall()

    assert [(row["name"], row["type"], row["pk"]) for row in columns] == [
        ("id", "INTEGER", 1),
        ("pair", "TEXT", 0),
        ("side", "TEXT", 0),
        ("quantity", "TEXT", 0),
        ("price", "TEXT", 0),
        ("fee", "TEXT", 0),
        ("filled_at", "TEXT", 0),
        ("created_at", "TEXT", 0),
    ]
    assert str(columns[-1]["dflt_value"]).lower() == "current_timestamp"


def test_insert_ledger_entry_appends_rows() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    writer.insert_ledger_entry(
        "DOGE/USD",
        "buy",
        Decimal("125.5"),
        Decimal("0.1234"),
        Decimal("0.05"),
        "2026-03-25T00:00:00Z",
    )
    writer.insert_ledger_entry(
        "DOGE/USD",
        "sell",
        Decimal("100"),
        Decimal("0.1300"),
        Decimal("0.04"),
        "2026-03-25T00:05:00Z",
    )

    rows = conn.execute(
        "SELECT id, pair, side, quantity, price, fee, filled_at, created_at "
        "FROM ledger ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0]["id"] == 1
    assert rows[0]["pair"] == "DOGE/USD"
    assert rows[0]["side"] == "buy"
    assert rows[0]["quantity"] == "125.5"
    assert rows[0]["price"] == "0.1234"
    assert rows[0]["fee"] == "0.05"
    assert rows[0]["filled_at"] == "2026-03-25T00:00:00Z"
    assert rows[0]["created_at"] is not None
    assert rows[1]["id"] == 2
    assert rows[1]["side"] == "sell"
