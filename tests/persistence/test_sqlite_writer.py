from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from persistence.sqlite import (
    SqlitePositionNotFoundError,
    SqliteWriter,
    ensure_schema,
)


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    return conn


def test_insert_position_creates_row() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    writer.insert_position("pos-1", "DOGE/USD")

    row = conn.execute(
        "SELECT position_id, pair, closed_at FROM positions WHERE position_id = ?",
        ("pos-1",),
    ).fetchone()
    assert row is not None
    assert row["position_id"] == "pos-1"
    assert row["pair"] == "DOGE/USD"
    assert row["closed_at"] is None


def test_insert_position_duplicate_is_ignored() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    writer.insert_position("pos-1", "DOGE/USD")
    writer.insert_position("pos-1", "BTC/USD")

    rows = conn.execute(
        "SELECT position_id, pair FROM positions WHERE position_id = ?",
        ("pos-1",),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["pair"] == "DOGE/USD"


def test_update_position_closed_sets_current_utc_timestamp() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)
    writer.insert_position("pos-1", "DOGE/USD")

    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    writer.update_position_closed("pos-1")
    after = datetime.now(timezone.utc) + timedelta(seconds=1)

    row = conn.execute(
        "SELECT closed_at FROM positions WHERE position_id = ?",
        ("pos-1",),
    ).fetchone()
    assert row is not None
    assert row["closed_at"] is not None

    closed_at = datetime.strptime(row["closed_at"], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc
    )
    assert before <= closed_at <= after


def test_update_position_closed_missing_position_raises_typed_error() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    with pytest.raises(SqlitePositionNotFoundError):
        writer.update_position_closed("missing")
