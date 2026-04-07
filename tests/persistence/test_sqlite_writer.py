from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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


def _insert_trade_outcome(
    writer: SqliteWriter,
    *,
    node_id: str,
    net_pnl: Decimal,
    node_depth: int,
) -> None:
    closed_at = datetime.now(timezone.utc)
    writer.insert_trade_outcome(
        node_id=node_id,
        pair="ETH/USD",
        direction="buy",
        entry_price=Decimal("100"),
        exit_price=Decimal("105"),
        entry_cost=Decimal("100"),
        exit_proceeds=Decimal("105"),
        net_pnl=net_pnl,
        fee_total=None,
        exit_reason="test",
        hold_hours=1.0,
        confidence=0.8,
        opened_at=(closed_at - timedelta(hours=1)).isoformat(),
        closed_at=closed_at.isoformat(),
        node_depth=node_depth,
    )


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


def test_fetch_child_trade_stats_filters_by_depth() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    _insert_trade_outcome(
        writer,
        node_id="root-usd",
        net_pnl=Decimal("5"),
        node_depth=0,
    )
    _insert_trade_outcome(
        writer,
        node_id="child-eth",
        net_pnl=Decimal("4"),
        node_depth=1,
    )
    _insert_trade_outcome(
        writer,
        node_id="child-sol",
        net_pnl=Decimal("-2"),
        node_depth=1,
    )

    wins, losses, payoff = writer.fetch_child_trade_stats(lookback_days=3650)

    assert wins == 1
    assert losses == 1
    assert payoff == Decimal("2")


def test_fetch_child_trade_stats_computes_payoff_ratio() -> None:
    conn = _memory_db()
    writer = SqliteWriter(conn)

    _insert_trade_outcome(
        writer,
        node_id="child-1",
        net_pnl=Decimal("6"),
        node_depth=1,
    )
    _insert_trade_outcome(
        writer,
        node_id="child-2",
        net_pnl=Decimal("4"),
        node_depth=1,
    )
    _insert_trade_outcome(
        writer,
        node_id="child-3",
        net_pnl=Decimal("-2"),
        node_depth=1,
    )
    _insert_trade_outcome(
        writer,
        node_id="child-4",
        net_pnl=Decimal("-3"),
        node_depth=1,
    )

    wins, losses, payoff = writer.fetch_child_trade_stats(lookback_days=3650)

    assert wins == 2
    assert losses == 2
    assert payoff == Decimal("2")
