from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

from core.types import Position, PositionSide
from persistence.sqlite import (
    SqliteReader,
    SqliteWriter,
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


def _insert_position(
    conn: sqlite3.Connection, position_id: str, pair: str, *, closed: bool = False
) -> None:
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
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
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


# ── Rehydration persistence tests ──────────────────────────────


def test_upsert_and_fetch_position() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    pos = Position(
        position_id="kbv4-1",
        pair="DOGE/USD",
        side=PositionSide.LONG,
        quantity=Decimal("100"),
        entry_price=Decimal("0.09"),
        stop_price=Decimal("0.085"),
        target_price=Decimal("0.10"),
    )
    writer.upsert_position(pos)
    positions = reader.fetch_open_positions()

    assert len(positions) == 1
    assert positions[0].position_id == "kbv4-1"
    assert positions[0].pair == "DOGE/USD"
    assert positions[0].side == PositionSide.LONG
    assert positions[0].quantity == Decimal("100")
    assert positions[0].entry_price == Decimal("0.09")
    assert positions[0].stop_price == Decimal("0.085")
    assert positions[0].target_price == Decimal("0.10")


def test_upsert_position_updates_on_conflict() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    pos = Position(
        position_id="kbv4-1",
        pair="DOGE/USD",
        side=PositionSide.LONG,
        quantity=Decimal("100"),
        entry_price=Decimal("0.09"),
        stop_price=Decimal("0.085"),
        target_price=Decimal("0.10"),
    )
    writer.upsert_position(pos)

    from dataclasses import replace

    updated = replace(pos, stop_price=Decimal("0.086"), quantity=Decimal("50"))
    writer.upsert_position(updated)

    positions = reader.fetch_open_positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("50")
    assert positions[0].stop_price == Decimal("0.086")


def test_closed_position_not_in_open_positions() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    pos = Position(
        position_id="kbv4-1",
        pair="DOGE/USD",
        side=PositionSide.LONG,
        quantity=Decimal("100"),
        entry_price=Decimal("0.09"),
        stop_price=Decimal("0.085"),
        target_price=Decimal("0.10"),
    )
    writer.upsert_position(pos)
    writer.update_position_closed("kbv4-1")

    assert reader.fetch_open_positions() == ()


def test_cooldown_set_and_fetch() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    writer.set_cooldown("DOGE/USD", "2026-03-30T12:00:00")
    cooldowns = reader.fetch_cooldowns()

    assert len(cooldowns) == 1
    assert cooldowns[0] == ("DOGE/USD", "2026-03-30T12:00:00")


def test_cooldown_clear() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    writer.set_cooldown("DOGE/USD", "2026-03-30T12:00:00")
    writer.clear_cooldown("DOGE/USD")

    assert reader.fetch_cooldowns() == ()


def test_upsert_order_and_fetch_open() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    writer.upsert_order(
        "ord-1",
        "DOGE/USD",
        "cl-1",
        kind="position_entry",
        side="buy",
        base_qty=Decimal("100"),
        quote_qty=Decimal("9"),
        exchange_order_id="EX-001",
    )
    orders = reader.fetch_open_orders()

    assert len(orders) == 1
    po, exch_oid = orders[0]
    assert po.client_order_id == "cl-1"
    assert po.kind == "position_entry"
    assert po.base_qty == Decimal("100")
    assert exch_oid == "EX-001"


def test_close_order_removes_from_open() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    writer.upsert_order("ord-1", "DOGE/USD", "cl-1", kind="position_entry", side="buy")
    writer.close_order("ord-1")

    assert reader.fetch_open_orders() == ()


def test_cancel_order_removes_from_open() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    writer.upsert_order("ord-1", "DOGE/USD", "cl-1", kind="position_entry", side="buy")
    writer.cancel_order("ord-1")

    assert reader.fetch_open_orders() == ()
