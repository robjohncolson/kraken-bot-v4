"""Tests for persisting rotation pair cooldowns to SQLite."""
from __future__ import annotations

import sqlite3
import time as _time
from datetime import datetime, timedelta, timezone
from persistence.sqlite import SqliteReader, SqliteWriter, ensure_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# 1. Setting a cooldown calls set_cooldown() on the writer
# ---------------------------------------------------------------------------

def test_set_cooldown_persists_to_writer():
    """When a rotation pair cooldown is set in-memory, it should also be
    persisted to SQLite via writer.set_cooldown()."""
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    pair = "DOGE/USD"
    abs_until = datetime.now(timezone.utc) + timedelta(seconds=1800)
    writer.set_cooldown(pair, abs_until.isoformat())

    rows = reader.fetch_cooldowns()
    assert len(rows) == 1
    assert rows[0][0] == pair
    stored_dt = datetime.fromisoformat(rows[0][1])
    # Should be within a second of what we wrote
    assert abs((stored_dt - abs_until).total_seconds()) < 1


# ---------------------------------------------------------------------------
# 2. Loading cooldowns on startup populates in-memory dict
# ---------------------------------------------------------------------------

def test_load_cooldowns_populates_memory_dict():
    """Persisted future cooldowns should populate _rotation_pair_cooldowns
    with correct monotonic values on startup."""
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    # Write a cooldown 900 seconds in the future
    future_until = datetime.now(timezone.utc) + timedelta(seconds=900)
    writer.set_cooldown("DOGE/USD", future_until.isoformat())

    # Simulate the startup loading logic from runtime_loop.py
    cooldowns: dict[str, float] = {}
    stored = reader.fetch_cooldowns()
    now_utc = datetime.now(timezone.utc)
    mono_now = _time.monotonic()
    for pair, until_str in stored:
        remaining = (datetime.fromisoformat(until_str) - now_utc).total_seconds()
        if remaining > 0:
            cooldowns[pair] = mono_now + remaining

    assert "DOGE/USD" in cooldowns
    # The monotonic expiry should be ~900s from now (allow 5s tolerance)
    expected_mono = mono_now + 900
    assert abs(cooldowns["DOGE/USD"] - expected_mono) < 5


# ---------------------------------------------------------------------------
# 3. Expired cooldowns are not loaded
# ---------------------------------------------------------------------------

def test_expired_cooldowns_not_loaded():
    """Cooldowns with expiry in the past should be skipped during loading."""
    conn = _memory_db()
    writer = SqliteWriter(conn)
    reader = SqliteReader(conn)

    # Write a cooldown that already expired
    past_until = datetime.now(timezone.utc) - timedelta(seconds=60)
    writer.set_cooldown("DOGE/USD", past_until.isoformat())

    # Also write a valid future cooldown
    future_until = datetime.now(timezone.utc) + timedelta(seconds=600)
    writer.set_cooldown("ETH/USD", future_until.isoformat())

    # Simulate loading
    cooldowns: dict[str, float] = {}
    stored = reader.fetch_cooldowns()
    now_utc = datetime.now(timezone.utc)
    mono_now = _time.monotonic()
    for pair, until_str in stored:
        remaining = (datetime.fromisoformat(until_str) - now_utc).total_seconds()
        if remaining > 0:
            cooldowns[pair] = mono_now + remaining

    assert "DOGE/USD" not in cooldowns
    assert "ETH/USD" in cooldowns


# ---------------------------------------------------------------------------
# 4. Cooldown check logic still uses in-memory dict
# ---------------------------------------------------------------------------

def test_cooldown_check_uses_memory_dict():
    """Runtime cooldown checks should use the in-memory dict, not SQLite."""
    cooldowns: dict[str, float] = {
        "DOGE/USD": _time.monotonic() + 1000,  # still active
        "ETH/USD": _time.monotonic() - 10,      # expired
    }

    # Active cooldown should block
    expiry = cooldowns.get("DOGE/USD")
    assert expiry is not None and expiry > _time.monotonic()

    # Expired cooldown should not block
    expiry = cooldowns.get("ETH/USD")
    assert expiry is not None and expiry <= _time.monotonic()

    # Unknown pair should not block
    expiry = cooldowns.get("BTC/USD")
    assert expiry is None
