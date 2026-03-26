"""Offline dataset export from the bot's SQLite database.

Provides a read-only ResearchReader that returns pandas DataFrames
suitable for analysis notebooks and CSV export pipelines.
"""

from __future__ import annotations

import sqlite3

import pandas as pd


class ResearchReadError(Exception):
    """Raised when a research read query fails."""


class ResearchReader:
    """Read-only adapter that returns DataFrames for offline analysis."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def fetch_fills(self) -> pd.DataFrame:
        """Return fills from the ledger table.

        Columns: pair, side, quantity, price, fee, filled_at.
        quantity/price/fee are Decimal strings in the DB.
        """
        query = (
            "SELECT pair, side, quantity, price, fee, filled_at "
            "FROM ledger ORDER BY filled_at"
        )
        try:
            return pd.read_sql_query(query, self._conn)
        except sqlite3.Error as exc:
            raise ResearchReadError(f"Failed to read fills: {exc}") from exc

    def fetch_orders(self) -> pd.DataFrame:
        """Return all orders.

        Columns: order_id, pair, position_id, exchange_order_id,
                 client_order_id, recorded_fee, created_at.
        """
        query = (
            "SELECT order_id, pair, position_id, exchange_order_id, "
            "client_order_id, recorded_fee, created_at "
            "FROM orders ORDER BY created_at"
        )
        try:
            return pd.read_sql_query(query, self._conn)
        except sqlite3.Error as exc:
            raise ResearchReadError(f"Failed to read orders: {exc}") from exc

    def fetch_closed_trades(self) -> pd.DataFrame:
        """Return closed positions (closed_at IS NOT NULL).

        Columns: position_id, pair, created_at, closed_at.
        """
        query = (
            "SELECT position_id, pair, created_at, closed_at "
            "FROM positions WHERE closed_at IS NOT NULL "
            "ORDER BY closed_at"
        )
        try:
            return pd.read_sql_query(query, self._conn)
        except sqlite3.Error as exc:
            raise ResearchReadError(
                f"Failed to read closed trades: {exc}"
            ) from exc


__all__ = ["ResearchReadError", "ResearchReader"]
