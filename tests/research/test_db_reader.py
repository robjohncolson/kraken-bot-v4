"""Tests for research.db_reader.ResearchReader."""

from __future__ import annotations

import sqlite3

import pytest

from persistence.sqlite import SCHEMA_STATEMENTS
from research.db_reader import ResearchReader


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """In-memory SQLite database with the bot schema applied."""
    db = sqlite3.connect(":memory:")
    for ddl in SCHEMA_STATEMENTS:
        db.execute(ddl)
    db.commit()
    return db


@pytest.fixture()
def reader(conn: sqlite3.Connection) -> ResearchReader:
    return ResearchReader(conn)


class TestFetchFills:
    def test_returns_expected_columns_and_rows(
        self, conn: sqlite3.Connection, reader: ResearchReader
    ) -> None:
        conn.execute(
            "INSERT INTO ledger (pair, side, quantity, price, fee, filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("XXBTZUSD", "buy", "0.5", "30000.00", "0.10", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO ledger (pair, side, quantity, price, fee, filled_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("XETHZUSD", "sell", "2.0", "2000.00", "0.05", "2026-01-02T00:00:00Z"),
        )
        conn.commit()

        df = reader.fetch_fills()

        assert df.shape == (2, 6)
        assert list(df.columns) == [
            "pair", "side", "quantity", "price", "fee", "filled_at",
        ]


class TestFetchOrders:
    def test_returns_expected_columns_and_rows(
        self, conn: sqlite3.Connection, reader: ResearchReader
    ) -> None:
        conn.execute(
            "INSERT INTO positions (position_id, pair) VALUES (?, ?)",
            ("pos-1", "XXBTZUSD"),
        )
        conn.execute(
            "INSERT INTO orders "
            "(order_id, pair, position_id, exchange_order_id, client_order_id, recorded_fee) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ord-1", "XXBTZUSD", "pos-1", "exch-1", "cli-1", "0.10"),
        )
        conn.execute(
            "INSERT INTO orders "
            "(order_id, pair, position_id, exchange_order_id, client_order_id, recorded_fee) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("ord-2", "XETHZUSD", None, "exch-2", "cli-2", "0.05"),
        )
        conn.commit()

        df = reader.fetch_orders()

        assert df.shape == (2, 7)
        assert list(df.columns) == [
            "order_id", "pair", "position_id", "exchange_order_id",
            "client_order_id", "recorded_fee", "created_at",
        ]


class TestFetchClosedTrades:
    def test_returns_only_closed_positions(
        self, conn: sqlite3.Connection, reader: ResearchReader
    ) -> None:
        conn.execute(
            "INSERT INTO positions (position_id, pair) VALUES (?, ?)",
            ("pos-open", "XXBTZUSD"),
        )
        conn.execute(
            "INSERT INTO positions (position_id, pair, closed_at) VALUES (?, ?, ?)",
            ("pos-closed", "XETHZUSD", "2026-01-15T12:00:00Z"),
        )
        conn.commit()

        df = reader.fetch_closed_trades()

        assert df.shape == (1, 4)
        assert list(df.columns) == [
            "position_id", "pair", "created_at", "closed_at",
        ]
        assert df.iloc[0]["position_id"] == "pos-closed"


class TestEmptyTables:
    def test_empty_fills(self, reader: ResearchReader) -> None:
        df = reader.fetch_fills()
        assert df.shape[0] == 0
        assert list(df.columns) == [
            "pair", "side", "quantity", "price", "fee", "filled_at",
        ]

    def test_empty_orders(self, reader: ResearchReader) -> None:
        df = reader.fetch_orders()
        assert df.shape[0] == 0
        assert list(df.columns) == [
            "order_id", "pair", "position_id", "exchange_order_id",
            "client_order_id", "recorded_fee", "created_at",
        ]

    def test_empty_closed_trades(self, reader: ResearchReader) -> None:
        df = reader.fetch_closed_trades()
        assert df.shape[0] == 0
        assert list(df.columns) == [
            "position_id", "pair", "created_at", "closed_at",
        ]
