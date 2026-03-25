"""SQLite persistence adapter for local-first bot coordination.

Provides schema bootstrap plus the minimal read/write interface for
positions and orders used by startup reconciliation.
"""

from __future__ import annotations

import logging
import sqlite3
from decimal import Decimal
from pathlib import Path

from core.errors import KrakenBotError
from trading.reconciler import RecordedOrder, RecordedPosition, RecordedState

logger = logging.getLogger(__name__)

POSITIONS_DDL = """\
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    pair        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
)"""

ORDERS_DDL = """\
CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,
    pair              TEXT NOT NULL,
    position_id       TEXT REFERENCES positions(position_id),
    exchange_order_id TEXT,
    client_order_id   TEXT,
    recorded_fee      TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
)"""

SCHEMA_STATEMENTS = (POSITIONS_DDL, ORDERS_DDL)


class SqlitePersistenceError(KrakenBotError):
    """Base exception for SQLite persistence failures."""


class SqliteOpenError(SqlitePersistenceError):
    """Raised when the database cannot be opened or configured."""


class SqliteSchemaError(SqlitePersistenceError):
    """Raised when schema bootstrap fails."""


class SqliteReadError(SqlitePersistenceError):
    """Raised when a read query fails."""


class SqliteWriteError(SqlitePersistenceError):
    """Raised when a write query fails."""


class SqlitePositionNotFoundError(SqliteWriteError):
    """Raised when a requested position does not exist."""

    def __init__(self, position_id: str) -> None:
        self.position_id = position_id
        super().__init__(f"Position {position_id!r} does not exist.")


def open_database(path: Path) -> sqlite3.Connection:
    """Open a SQLite database with WAL mode and sensible defaults.

    Creates parent directories if needed. Raises SqliteOpenError on failure.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        logger.info("Opened SQLite database: %s", path.resolve())
        return conn
    except (sqlite3.Error, OSError) as exc:
        raise SqliteOpenError(f"Failed to open database at {path}: {exc}") from exc


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create positions and orders tables if they don't exist."""
    try:
        for ddl in SCHEMA_STATEMENTS:
            conn.execute(ddl)
        conn.commit()
        logger.info("SQLite schema verified (positions, orders)")
    except sqlite3.Error as exc:
        raise SqliteSchemaError(f"Schema bootstrap failed: {exc}") from exc


class SqliteReader:
    """Read-only adapter for fetching recorded state from SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def fetch_positions(self) -> tuple[RecordedPosition, ...]:
        """Fetch all open positions (closed_at IS NULL)."""
        try:
            cursor = self._conn.execute(
                "SELECT position_id, pair FROM positions WHERE closed_at IS NULL "
                "ORDER BY position_id"
            )
            return tuple(
                RecordedPosition(
                    position_id=row["position_id"],
                    pair=row["pair"],
                )
                for row in cursor
            )
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read positions: {exc}") from exc

    def fetch_orders(self) -> tuple[RecordedOrder, ...]:
        """Fetch all orders."""
        try:
            cursor = self._conn.execute(
                "SELECT order_id, pair, position_id, exchange_order_id, "
                "client_order_id, recorded_fee FROM orders ORDER BY order_id"
            )
            return tuple(
                RecordedOrder(
                    order_id=row["order_id"],
                    pair=row["pair"],
                    position_id=row["position_id"] or None,
                    exchange_order_id=row["exchange_order_id"] or None,
                    client_order_id=row["client_order_id"] or None,
                    recorded_fee=Decimal(row["recorded_fee"])
                    if row["recorded_fee"] is not None
                    else None,
                )
                for row in cursor
            )
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read orders: {exc}") from exc

    def fetch_recorded_state(self) -> RecordedState:
        """Fetch positions and orders as a RecordedState for reconciliation."""
        positions = self.fetch_positions()
        orders = self.fetch_orders()
        logger.info(
            "Fetched recorded state: %d open positions, %d orders",
            len(positions),
            len(orders),
        )
        return RecordedState(positions=positions, orders=orders)


class SqliteWriter:
    """Write adapter for idempotent position mutations."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def insert_position(self, position_id: str, pair: str) -> None:
        """Insert a position record if it does not already exist."""
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO positions (position_id, pair) VALUES (?, ?)",
                (position_id, pair),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            raise SqliteWriteError(f"Failed to insert position {position_id!r}: {exc}") from exc

    def update_position_closed(self, position_id: str) -> None:
        """Mark an existing position closed with the current UTC timestamp."""
        try:
            cursor = self._conn.execute(
                "UPDATE positions "
                "SET closed_at = COALESCE(closed_at, datetime('now')) "
                "WHERE position_id = ?",
                (position_id,),
            )
        except sqlite3.Error as exc:
            raise SqliteWriteError(
                f"Failed to update closed_at for position {position_id!r}: {exc}"
            ) from exc

        if cursor.rowcount == 0:
            self._conn.rollback()
            raise SqlitePositionNotFoundError(position_id)

        self._conn.commit()


__all__ = [
    "SqliteOpenError",
    "SqlitePersistenceError",
    "SqlitePositionNotFoundError",
    "SqliteReadError",
    "SqliteReader",
    "SqliteSchemaError",
    "SqliteWriteError",
    "SqliteWriter",
    "ensure_schema",
    "open_database",
]
