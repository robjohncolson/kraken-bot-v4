"""SQLite persistence adapter for local-first bot coordination.

Provides schema bootstrap plus the minimal read/write interface for
positions and orders used by startup reconciliation.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from core.errors import KrakenBotError
from core.types import (
    OrderSide,
    PendingOrder,
    Position,
    PositionSide,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
    ZERO_DECIMAL,
)
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

LEDGER_DDL = """\
CREATE TABLE IF NOT EXISTS ledger (
    id         INTEGER PRIMARY KEY,
    pair       TEXT,
    side       TEXT,
    quantity   TEXT,
    price      TEXT,
    fee        TEXT,
    filled_at  TEXT,
    created_at TEXT DEFAULT current_timestamp
)"""

ROTATION_NODES_DDL = """\
CREATE TABLE IF NOT EXISTS rotation_nodes (
    node_id         TEXT PRIMARY KEY,
    parent_node_id  TEXT,
    depth           INTEGER NOT NULL DEFAULT 0,
    asset           TEXT NOT NULL,
    quantity_total  TEXT NOT NULL DEFAULT '0',
    quantity_free   TEXT NOT NULL DEFAULT '0',
    quantity_reserved TEXT NOT NULL DEFAULT '0',
    entry_pair      TEXT,
    from_asset      TEXT,
    order_side      TEXT,
    entry_price     TEXT,
    position_id     TEXT,
    opened_at       TEXT,
    deadline_at     TEXT,
    window_hours    REAL,
    confidence      REAL DEFAULT 0.0,
    status          TEXT NOT NULL DEFAULT 'planned',
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
)"""

COOLDOWNS_DDL = """\
CREATE TABLE IF NOT EXISTS cooldowns (
    pair           TEXT PRIMARY KEY,
    cooldown_until TEXT NOT NULL
)"""

PAIR_METADATA_DDL = """\
CREATE TABLE IF NOT EXISTS pair_metadata (
    pair         TEXT PRIMARY KEY,
    ordermin     TEXT NOT NULL,
    lot_decimals INTEGER,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
)"""

TRADE_OUTCOMES_DDL = """\
CREATE TABLE IF NOT EXISTS trade_outcomes (
    id            INTEGER PRIMARY KEY,
    node_id       TEXT NOT NULL,
    pair          TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_price   TEXT NOT NULL,
    exit_price    TEXT NOT NULL,
    entry_cost    TEXT NOT NULL,
    exit_proceeds TEXT NOT NULL,
    net_pnl       TEXT NOT NULL,
    fee_total     TEXT,
    exit_reason   TEXT NOT NULL,
    hold_hours    REAL,
    confidence    REAL,
    opened_at     TEXT NOT NULL,
    closed_at     TEXT NOT NULL,
    node_depth    INTEGER NOT NULL DEFAULT 0
)"""

SCHEMA_STATEMENTS = (
    POSITIONS_DDL,
    ORDERS_DDL,
    LEDGER_DDL,
    COOLDOWNS_DDL,
    ROTATION_NODES_DDL,
    PAIR_METADATA_DDL,
    TRADE_OUTCOMES_DDL,
)

# Columns added after initial schema — safe to run repeatedly.
_POSITION_MIGRATIONS = (
    ("side", "TEXT DEFAULT 'long'"),
    ("quantity", "TEXT DEFAULT '0'"),
    ("entry_price", "TEXT DEFAULT '0'"),
    ("stop_price", "TEXT DEFAULT '0'"),
    ("target_price", "TEXT DEFAULT '0'"),
)

_ORDER_MIGRATIONS = (
    ("kind", "TEXT DEFAULT ''"),
    ("side", "TEXT DEFAULT ''"),
    ("base_qty", "TEXT DEFAULT '0'"),
    ("filled_qty", "TEXT DEFAULT '0'"),
    ("quote_qty", "TEXT DEFAULT '0'"),
    ("limit_price", "TEXT"),
    ("status", "TEXT DEFAULT 'open'"),
    ("rotation_node_id", "TEXT"),
)

_ROTATION_NODE_MIGRATIONS = (
    ("entry_cost", "TEXT"),
    ("fill_price", "TEXT"),
    ("exit_price", "TEXT"),
    ("closed_at", "TEXT"),
    ("exit_proceeds", "TEXT"),
    ("take_profit_price", "TEXT"),
    ("stop_loss_price", "TEXT"),
    ("trailing_stop_high", "TEXT"),
    ("exit_reason", "TEXT"),
    ("ta_direction", "TEXT"),
    ("recovery_count", "INTEGER DEFAULT 0"),
)

_TRADE_OUTCOME_MIGRATIONS = (
    ("node_depth", "INTEGER DEFAULT 0"),
)


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
    """Create tables and run column migrations."""
    try:
        for ddl in SCHEMA_STATEMENTS:
            conn.execute(ddl)
        _migrate_columns(conn, "positions", _POSITION_MIGRATIONS)
        _migrate_columns(conn, "orders", _ORDER_MIGRATIONS)
        _migrate_columns(conn, "rotation_nodes", _ROTATION_NODE_MIGRATIONS)
        _migrate_columns(conn, "trade_outcomes", _TRADE_OUTCOME_MIGRATIONS)
        conn.commit()
        logger.info(
            "SQLite schema verified (positions, orders, ledger, cooldowns, rotation_nodes, pair_metadata, trade_outcomes)"
        )
    except sqlite3.Error as exc:
        raise SqliteSchemaError(f"Schema bootstrap failed: {exc}") from exc


def _migrate_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[tuple[str, str], ...],
) -> None:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for col_name, col_def in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")
            logger.info("Migrated %s: added column %s", table, col_name)


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

    def fetch_open_positions(self) -> tuple[Position, ...]:
        """Fetch open positions with full runtime fields for rehydration."""
        try:
            cursor = self._conn.execute(
                "SELECT position_id, pair, side, quantity, entry_price, "
                "stop_price, target_price FROM positions WHERE closed_at IS NULL "
                "ORDER BY position_id"
            )
            return tuple(
                Position(
                    position_id=row["position_id"],
                    pair=row["pair"],
                    side=PositionSide(row["side"])
                    if row["side"]
                    else PositionSide.LONG,
                    quantity=Decimal(row["quantity"])
                    if row["quantity"]
                    else ZERO_DECIMAL,
                    entry_price=Decimal(row["entry_price"])
                    if row["entry_price"]
                    else ZERO_DECIMAL,
                    stop_price=Decimal(row["stop_price"])
                    if row["stop_price"]
                    else ZERO_DECIMAL,
                    target_price=Decimal(row["target_price"])
                    if row["target_price"]
                    else ZERO_DECIMAL,
                )
                for row in cursor
            )
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read open positions: {exc}") from exc

    def fetch_open_orders(self) -> tuple[tuple[PendingOrder, str | None], ...]:
        """Fetch tracked open orders for rehydration.

        Returns (PendingOrder, exchange_order_id) pairs so startup can
        match against Kraken open orders by exchange ID (Starter tier
        has no cl_ord_id).
        """
        try:
            cursor = self._conn.execute(
                "SELECT order_id, pair, position_id, client_order_id, "
                "exchange_order_id, kind, side, base_qty, filled_qty, "
                "quote_qty, limit_price "
                "FROM orders WHERE status = 'open' ORDER BY order_id"
            )
            return tuple(
                (
                    PendingOrder(
                        client_order_id=row["client_order_id"] or row["order_id"],
                        kind=row["kind"] or "position_entry",
                        pair=row["pair"],
                        side=row["side"] or "buy",
                        base_qty=Decimal(row["base_qty"])
                        if row["base_qty"]
                        else ZERO_DECIMAL,
                        filled_qty=Decimal(row["filled_qty"])
                        if row["filled_qty"]
                        else ZERO_DECIMAL,
                        quote_qty=Decimal(row["quote_qty"])
                        if row["quote_qty"]
                        else ZERO_DECIMAL,
                        position_id=row["position_id"] or "",
                    ),
                    row["exchange_order_id"],
                )
                for row in cursor
            )
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read open orders: {exc}") from exc

    def fetch_cooldowns(self) -> tuple[tuple[str, str], ...]:
        """Fetch active cooldowns."""
        try:
            cursor = self._conn.execute(
                "SELECT pair, cooldown_until FROM cooldowns ORDER BY pair"
            )
            return tuple((row["pair"], row["cooldown_until"]) for row in cursor)
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read cooldowns: {exc}") from exc

    def fetch_rotation_tree(self) -> RotationTreeState:
        """Fetch persisted rotation tree state."""
        try:
            cursor = self._conn.execute(
                "SELECT * FROM rotation_nodes WHERE status IN ('planned', 'open', 'closing', 'expired') "
                "ORDER BY depth, node_id"
            )
            nodes: list[RotationNode] = []
            root_ids: list[str] = []
            for row in cursor:
                node = RotationNode(
                    node_id=row["node_id"],
                    parent_node_id=row["parent_node_id"],
                    depth=row["depth"],
                    asset=row["asset"],
                    quantity_total=Decimal(row["quantity_total"]),
                    quantity_free=Decimal(row["quantity_free"]),
                    quantity_reserved=Decimal(row["quantity_reserved"] or "0"),
                    entry_pair=row["entry_pair"],
                    from_asset=row["from_asset"],
                    order_side=OrderSide(row["order_side"])
                    if row["order_side"]
                    else None,
                    entry_price=Decimal(row["entry_price"])
                    if row["entry_price"]
                    else None,
                    position_id=row["position_id"],
                    deadline_at=datetime.fromisoformat(row["deadline_at"])
                    if row["deadline_at"]
                    else None,
                    window_hours=row["window_hours"],
                    confidence=row["confidence"] or 0.0,
                    status=RotationNodeStatus(row["status"]),
                    entry_cost=Decimal(row["entry_cost"])
                    if row["entry_cost"]
                    else None,
                    fill_price=Decimal(row["fill_price"])
                    if row["fill_price"]
                    else None,
                    exit_price=Decimal(row["exit_price"])
                    if row["exit_price"]
                    else None,
                    closed_at=datetime.fromisoformat(row["closed_at"])
                    if row["closed_at"]
                    else None,
                    exit_proceeds=Decimal(row["exit_proceeds"])
                    if row["exit_proceeds"]
                    else None,
                    take_profit_price=Decimal(row["take_profit_price"])
                    if row["take_profit_price"]
                    else None,
                    stop_loss_price=Decimal(row["stop_loss_price"])
                    if row["stop_loss_price"]
                    else None,
                    trailing_stop_high=Decimal(row["trailing_stop_high"])
                    if row["trailing_stop_high"]
                    else None,
                    exit_reason=row["exit_reason"],
                    ta_direction=row["ta_direction"],
                    recovery_count=row["recovery_count"] or 0,
                )
                nodes.append(node)
                if node.parent_node_id is None:
                    root_ids.append(node.node_id)
            return RotationTreeState(
                nodes=tuple(nodes),
                root_node_ids=tuple(root_ids),
            )
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read rotation tree: {exc}") from exc

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

    def fetch_trade_outcomes(
        self,
        lookback_days: int = 30,
    ) -> tuple[sqlite3.Row, ...]:
        """Fetch recent trade outcomes, newest first."""
        horizon_days = max(0, lookback_days)
        try:
            cursor = self._conn.execute(
                "SELECT * FROM trade_outcomes "
                "WHERE julianday(closed_at) >= julianday('now', ?) "
                "ORDER BY julianday(closed_at) DESC, id DESC",
                (f"-{horizon_days} days",),
            )
            return tuple(cursor.fetchall())
        except sqlite3.Error as exc:
            raise SqliteReadError(f"Failed to read trade outcomes: {exc}") from exc


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
            raise SqliteWriteError(
                f"Failed to insert position {position_id!r}: {exc}"
            ) from exc

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
        """Insert an order record if it does not already exist."""
        try:
            self._conn.execute(
                "INSERT OR IGNORE INTO orders ("
                "order_id, pair, position_id, exchange_order_id, client_order_id, recorded_fee"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    pair,
                    position_id,
                    exchange_order_id,
                    client_order_id,
                    str(recorded_fee) if recorded_fee is not None else None,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to insert order {order_id!r}: {exc}"
            ) from exc

    def insert_ledger_entry(
        self,
        pair: str,
        side: str,
        quantity: Decimal | str,
        price: Decimal | str,
        fee: Decimal | str,
        filled_at: str,
    ) -> None:
        """Append a fill record to the ledger."""
        try:
            self._conn.execute(
                "INSERT INTO ledger (pair, side, quantity, price, fee, filled_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (pair, side, str(quantity), str(price), str(fee), filled_at),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to insert ledger entry for {pair!r}: {exc}"
            ) from exc

    def insert_trade_outcome(
        self,
        *,
        node_id: str,
        pair: str,
        direction: str,
        entry_price: Decimal | str,
        exit_price: Decimal | str,
        entry_cost: Decimal | str,
        exit_proceeds: Decimal | str,
        net_pnl: Decimal | str,
        fee_total: Decimal | str | None,
        exit_reason: str,
        hold_hours: float | None,
        confidence: float | None,
        opened_at: str,
        closed_at: str,
        node_depth: int = 0,
    ) -> None:
        """Append a settled trade outcome."""
        try:
            self._conn.execute(
                "INSERT INTO trade_outcomes ("
                "node_id, pair, direction, entry_price, exit_price, entry_cost, "
                "exit_proceeds, net_pnl, fee_total, exit_reason, hold_hours, "
                "confidence, opened_at, closed_at, node_depth"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node_id,
                    pair,
                    direction,
                    str(entry_price),
                    str(exit_price),
                    str(entry_cost),
                    str(exit_proceeds),
                    str(net_pnl),
                    str(fee_total) if fee_total is not None else None,
                    exit_reason,
                    hold_hours,
                    confidence,
                    opened_at,
                    closed_at,
                    node_depth,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to insert trade outcome for node {node_id!r}: {exc}"
            ) from exc

    def fetch_child_trade_stats(self, lookback_days: int = 90) -> tuple[int, int, Decimal]:
        """Return (wins, losses, avg_payoff_ratio) for child trades (depth > 0)."""
        try:
            cursor = self._conn.execute(
                "SELECT net_pnl FROM trade_outcomes "
                "WHERE node_depth > 0 "
                "AND julianday(closed_at) >= julianday('now', ?)",
                (f"-{lookback_days} days",),
            )
            rows = cursor.fetchall()
        except sqlite3.Error:
            return (0, 0, Decimal("1"))
        wins = losses = 0
        win_sum = loss_sum = Decimal("0")
        for row in rows:
            pnl = Decimal(str(row[0]))
            if pnl > 0:
                wins += 1
                win_sum += pnl
            else:
                losses += 1
                loss_sum += abs(pnl)
        if losses == 0 or wins == 0:
            return (wins, losses, Decimal("1"))
        return (wins, losses, (win_sum / wins) / (loss_sum / losses))

    def upsert_position(self, position: Position) -> None:
        """Insert or update a full position record."""
        try:
            self._conn.execute(
                "INSERT INTO positions (position_id, pair, side, quantity, "
                "entry_price, stop_price, target_price) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(position_id) DO UPDATE SET "
                "quantity=excluded.quantity, stop_price=excluded.stop_price, "
                "target_price=excluded.target_price",
                (
                    position.position_id,
                    position.pair,
                    position.side.value,
                    str(position.quantity),
                    str(position.entry_price),
                    str(position.stop_price),
                    str(position.target_price),
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to upsert position {position.position_id!r}: {exc}"
            ) from exc

    def upsert_order(
        self,
        order_id: str,
        pair: str,
        client_order_id: str,
        *,
        kind: str = "",
        side: str = "",
        base_qty: Decimal | str = ZERO_DECIMAL,
        filled_qty: Decimal | str = ZERO_DECIMAL,
        quote_qty: Decimal | str = ZERO_DECIMAL,
        limit_price: Decimal | str | None = None,
        position_id: str | None = None,
        exchange_order_id: str | None = None,
        rotation_node_id: str | None = None,
    ) -> None:
        """Insert or update a tracked order."""
        try:
            self._conn.execute(
                "INSERT INTO orders (order_id, pair, position_id, exchange_order_id, "
                "client_order_id, kind, side, base_qty, filled_qty, quote_qty, "
                "limit_price, status, rotation_node_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?) "
                "ON CONFLICT(order_id) DO UPDATE SET "
                "filled_qty=excluded.filled_qty, status=excluded.status",
                (
                    order_id,
                    pair,
                    position_id,
                    exchange_order_id,
                    client_order_id,
                    kind,
                    side,
                    str(base_qty),
                    str(filled_qty),
                    str(quote_qty),
                    str(limit_price) if limit_price is not None else None,
                    rotation_node_id,
                ),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to upsert order {order_id!r}: {exc}"
            ) from exc

    def close_order(self, order_id: str) -> None:
        """Mark an order as filled/closed."""
        try:
            self._conn.execute(
                "UPDATE orders SET status = 'filled' WHERE order_id = ?",
                (order_id,),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to close order {order_id!r}: {exc}"
            ) from exc

    def cancel_order(self, order_id: str) -> None:
        """Mark an order as cancelled."""
        try:
            self._conn.execute(
                "UPDATE orders SET status = 'cancelled' WHERE order_id = ?",
                (order_id,),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to cancel order {order_id!r}: {exc}"
            ) from exc

    def set_cooldown(self, pair: str, cooldown_until: str) -> None:
        """Upsert a cooldown for a pair."""
        try:
            self._conn.execute(
                "INSERT INTO cooldowns (pair, cooldown_until) VALUES (?, ?) "
                "ON CONFLICT(pair) DO UPDATE SET cooldown_until=excluded.cooldown_until",
                (pair, cooldown_until),
            )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to set cooldown for {pair!r}: {exc}"
            ) from exc

    def clear_cooldown(self, pair: str) -> None:
        """Remove a cooldown for a pair."""
        try:
            self._conn.execute("DELETE FROM cooldowns WHERE pair = ?", (pair,))
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(
                f"Failed to clear cooldown for {pair!r}: {exc}"
            ) from exc

    def save_rotation_tree(self, tree: RotationTreeState) -> None:
        """Persist the full rotation tree state (replace all live nodes + closed with P&L)."""
        try:
            # Clear stale live nodes
            self._conn.execute(
                "DELETE FROM rotation_nodes WHERE status IN ('planned', 'open', 'closing', 'expired')"
            )
            for node in tree.nodes:
                if node.status not in (
                    RotationNodeStatus.PLANNED,
                    RotationNodeStatus.OPEN,
                    RotationNodeStatus.CLOSING,
                    RotationNodeStatus.CLOSED,
                    RotationNodeStatus.EXPIRED,
                ):
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO rotation_nodes ("
                    "node_id, parent_node_id, depth, asset, quantity_total, "
                    "quantity_free, quantity_reserved, entry_pair, from_asset, "
                    "order_side, entry_price, position_id, deadline_at, "
                    "window_hours, confidence, status, "
                    "entry_cost, fill_price, exit_price, closed_at, exit_proceeds, "
                    "take_profit_price, stop_loss_price, trailing_stop_high, exit_reason, ta_direction, recovery_count"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        node.node_id,
                        node.parent_node_id,
                        node.depth,
                        node.asset,
                        str(node.quantity_total),
                        str(node.quantity_free),
                        str(node.quantity_reserved),
                        node.entry_pair,
                        node.from_asset,
                        node.order_side.value if node.order_side else None,
                        str(node.entry_price) if node.entry_price else None,
                        node.position_id,
                        node.deadline_at.isoformat() if node.deadline_at else None,
                        node.window_hours,
                        node.confidence,
                        node.status.value,
                        str(node.entry_cost) if node.entry_cost else None,
                        str(node.fill_price) if node.fill_price else None,
                        str(node.exit_price) if node.exit_price else None,
                        node.closed_at.isoformat() if node.closed_at else None,
                        str(node.exit_proceeds) if node.exit_proceeds else None,
                        str(node.take_profit_price) if node.take_profit_price else None,
                        str(node.stop_loss_price) if node.stop_loss_price else None,
                        str(node.trailing_stop_high)
                        if node.trailing_stop_high
                        else None,
                        node.exit_reason,
                        node.ta_direction,
                        node.recovery_count,
                    ),
                )
            self._conn.commit()
        except sqlite3.Error as exc:
            self._conn.rollback()
            raise SqliteWriteError(f"Failed to save rotation tree: {exc}") from exc

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
