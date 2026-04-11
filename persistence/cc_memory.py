"""CC temporal memory — a structured, time-linked event log for the trading brain.

Stores decisions, observations, portfolio snapshots, regime states, and
parameter changes. Each memory is timestamped, categorized, and queryable.

CC writes memories during review cycles. Future CC sessions read them to
understand what happened, when, and why — building continuity across sessions.

Usage:
    from persistence.cc_memory import CCMemory
    mem = CCMemory("data/bot.db")

    # Write
    mem.record_decision("AVAX/USD", "buy", {"reason": "RSI=15, 4H up, Kronos bullish", "size_usd": 10})
    mem.record_observation("market", {"note": "Most pairs ranging, only AERO trending"})
    mem.record_portfolio_snapshot({"cash_usd": 152.81, "total_value": 245.0, "open_positions": 3})
    mem.record_regime("SOL/USD", {"regime": "ranging", "trade_gate": 0.01})
    mem.record_param_change("ROTATION_MIN_CONFIDENCE", "0.65", "0.75", "Post-mortem: too many low-conf entries")

    # Read
    recent = mem.query(category="decision", hours=24)
    all_regimes = mem.query(category="regime", pair="SOL/USD", hours=168)
    snapshots = mem.query(category="portfolio_snapshot", hours=720)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cc_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    category TEXT NOT NULL,
    pair TEXT,
    content TEXT NOT NULL,
    importance REAL DEFAULT 0.5
);
CREATE INDEX IF NOT EXISTS idx_cc_memory_cat_ts ON cc_memory (category, timestamp);
CREATE INDEX IF NOT EXISTS idx_cc_memory_pair ON cc_memory (pair, timestamp);
"""


class CCMemory:
    """Persistent temporal memory for the CC trading brain."""

    def __init__(self, db_path: str | sqlite3.Connection) -> None:
        if isinstance(db_path, sqlite3.Connection):
            self._conn = db_path
        else:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            logger.warning("CC memory schema init failed: %s", exc)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _write(self, category: str, content: dict, pair: str | None = None, importance: float = 0.5) -> int:
        ts = self._now()
        try:
            cursor = self._conn.execute(
                "INSERT INTO cc_memory (timestamp, category, pair, content, importance) VALUES (?, ?, ?, ?, ?)",
                (ts, category, pair, json.dumps(content), importance),
            )
            self._conn.commit()
            return cursor.lastrowid or 0
        except sqlite3.Error as exc:
            logger.warning("CC memory write failed: %s", exc)
            return 0

    # === Write Methods ===

    def record_decision(self, pair: str, action: str, details: dict, importance: float = 0.7) -> int:
        """Record a trading decision (buy/sell/hold/skip)."""
        return self._write("decision", {"action": action, **details}, pair=pair, importance=importance)

    def record_observation(self, subject: str, details: dict, importance: float = 0.5) -> int:
        """Record a market observation or insight."""
        return self._write("observation", details, pair=subject if "/" in subject else None, importance=importance)

    def record_portfolio_snapshot(self, snapshot: dict, importance: float = 0.3) -> int:
        """Record a point-in-time portfolio state."""
        return self._write("portfolio_snapshot", snapshot, importance=importance)

    def record_regime(self, pair: str, regime_data: dict, importance: float = 0.4) -> int:
        """Record a regime detection result."""
        return self._write("regime", regime_data, pair=pair, importance=importance)

    def record_param_change(self, param: str, old_value: str, new_value: str, reason: str, importance: float = 0.8) -> int:
        """Record a strategy parameter change."""
        return self._write("param_change", {
            "param": param, "old": old_value, "new": new_value, "reason": reason,
        }, importance=importance)

    def record_postmortem(self, summary: dict, importance: float = 0.6) -> int:
        """Record post-mortem analysis results."""
        return self._write("postmortem", summary, importance=importance)

    # === Read Methods ===

    def query(
        self,
        *,
        category: str | None = None,
        pair: str | None = None,
        hours: int = 24,
        limit: int = 100,
        min_importance: float = 0.0,
    ) -> list[dict]:
        """Query memories by category, pair, and recency."""
        conditions = [f"julianday(timestamp) >= julianday('now', '-{hours} hours')"]
        params: list = []

        if category:
            conditions.append("category = ?")
            params.append(category)
        if pair:
            conditions.append("pair = ?")
            params.append(pair)
        if min_importance > 0:
            conditions.append("importance >= ?")
            params.append(min_importance)

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM cc_memory WHERE {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        try:
            rows = self._conn.execute(sql, params).fetchall()
            return [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "category": row["category"],
                    "pair": row["pair"],
                    "content": json.loads(row["content"]),
                    "importance": row["importance"],
                }
                for row in rows
            ]
        except sqlite3.Error as exc:
            logger.warning("CC memory query failed: %s", exc)
            return []

    def recent_decisions(self, hours: int = 24) -> list[dict]:
        """Shortcut: recent trading decisions."""
        return self.query(category="decision", hours=hours)

    def portfolio_history(self, hours: int = 168) -> list[dict]:
        """Shortcut: portfolio snapshots over the last week."""
        return self.query(category="portfolio_snapshot", hours=hours)

    def regime_history(self, pair: str, hours: int = 168) -> list[dict]:
        """Shortcut: regime states for a pair over the last week."""
        return self.query(category="regime", pair=pair, hours=hours)

    def important_memories(self, hours: int = 168) -> list[dict]:
        """Shortcut: high-importance memories from the last week."""
        return self.query(hours=hours, min_importance=0.7)

    def count(self, category: str | None = None) -> int:
        """Count memories, optionally by category."""
        if category:
            row = self._conn.execute("SELECT COUNT(*) FROM cc_memory WHERE category = ?", (category,)).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM cc_memory").fetchone()
        return row[0] if row else 0
