"""Dynamic cache for Kraken pair metadata (ordermin, lot_decimals).

Replaces the hardcoded KRAKEN_MINIMUM_ORDER_QUANTITIES map in grid/sizing.py
with a live cache that covers ALL tradable pairs.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Final
from exchange.symbols import normalize_pair

logger = logging.getLogger(__name__)

KRAKEN_ASSET_PAIRS_URL: Final[str] = "https://api.kraken.com/0/public/AssetPairs"
_REQUEST_TIMEOUT_SECONDS: Final[float] = 15.0


class PairMetadataCache:
    """Thread-safe cache for Kraken pair metadata backed by SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()
        self._cache: dict[str, _PairMeta] = {}
        self._last_refresh: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Fetch metadata from Kraken and persist to SQLite + memory."""
        try:
            raw = _fetch_asset_pairs()
        except Exception:
            logger.warning(
                "Failed to fetch AssetPairs from Kraken; falling back to DB cache",
                exc_info=True,
            )
            self.load_from_db()
            return

        now = datetime.now(timezone.utc).isoformat()
        rows: list[tuple[str, str, int, str]] = []
        new_cache: dict[str, _PairMeta] = {}

        for raw_name, info in raw.items():
            try:
                pair = normalize_pair(raw_name)
            except Exception:
                # Skip pairs that can't be normalized (e.g. exotic bases)
                continue

            ordermin_str = info.get("ordermin")
            lot_decimals_raw = info.get("lot_decimals")

            if ordermin_str is None or lot_decimals_raw is None:
                continue

            try:
                ordermin = Decimal(str(ordermin_str))
                lot_decimals = int(lot_decimals_raw)
            except (InvalidOperation, ValueError, TypeError):
                logger.warning("Skipping %s: bad ordermin/lot_decimals", raw_name)
                continue

            meta = _PairMeta(ordermin=ordermin, lot_decimals=lot_decimals)
            new_cache[pair] = meta
            rows.append((pair, str(ordermin), lot_decimals, now))

        with self._lock:
            self._cache = new_cache
            self._last_refresh = time.monotonic()

        _persist_rows(self._conn, rows)
        logger.info("PairMetadataCache refreshed: %d pairs", len(new_cache))

    def load_from_db(self) -> None:
        """Populate in-memory cache from SQLite (cold-start path)."""
        loaded: dict[str, _PairMeta] = {}
        try:
            cursor = self._conn.execute(
                "SELECT pair, ordermin, lot_decimals FROM pair_metadata"
            )
            for pair, ordermin_str, lot_dec in cursor.fetchall():
                try:
                    loaded[pair] = _PairMeta(
                        ordermin=Decimal(ordermin_str),
                        lot_decimals=int(lot_dec),
                    )
                except (InvalidOperation, ValueError, TypeError):
                    logger.warning("Skipping corrupt DB row for %s", pair)
        except sqlite3.OperationalError:
            # Table may not exist yet — not an error on first run
            logger.debug("pair_metadata table not found; cache remains empty")

        with self._lock:
            self._cache = loaded
            if loaded:
                self._last_refresh = time.monotonic()

        logger.info("PairMetadataCache loaded from DB: %d pairs", len(loaded))

    def ordermin(self, pair: str) -> Decimal | None:
        """Return the minimum order quantity for *pair*, or None if unknown."""
        with self._lock:
            meta = self._cache.get(pair)
        return meta.ordermin if meta is not None else None

    def lot_decimals(self, pair: str) -> int | None:
        """Return the lot decimal precision for *pair*, or None if unknown."""
        with self._lock:
            meta = self._cache.get(pair)
        return meta.lot_decimals if meta is not None else None

    def meets_minimum(self, pair: str, quantity: Decimal) -> bool:
        """Return True if *quantity* >= ordermin.  Fail-open if pair is unknown."""
        minimum = self.ordermin(pair)
        if minimum is None:
            return True
        return quantity >= minimum

    def stale(self, max_age_hours: float = 24.0) -> bool:
        """Return True if the cache has never been refreshed or is too old."""
        if self._last_refresh is None:
            return True
        elapsed_hours = (time.monotonic() - self._last_refresh) / 3600.0
        return elapsed_hours >= max_age_hours

    @property
    def pair_count(self) -> int:
        """Return the number of pairs currently cached."""
        with self._lock:
            return len(self._cache)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

class _PairMeta:
    """Lightweight container for a single pair's metadata."""

    __slots__ = ("ordermin", "lot_decimals")

    def __init__(self, *, ordermin: Decimal, lot_decimals: int) -> None:
        self.ordermin = ordermin
        self.lot_decimals = lot_decimals


def _fetch_asset_pairs() -> dict[str, dict]:
    """Call Kraken public AssetPairs and return the result dict."""
    req = urllib.request.Request(KRAKEN_ASSET_PAIRS_URL)
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    errors = body.get("error", [])
    if errors:
        raise RuntimeError(f"Kraken AssetPairs returned errors: {errors}")

    result: dict[str, dict] = body.get("result", {})
    if not result:
        raise RuntimeError("Kraken AssetPairs returned empty result")

    return result


def _persist_rows(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, int, str]],
) -> None:
    """Write rows to the pair_metadata table inside a single transaction."""
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO pair_metadata "
            "(pair, ordermin, lot_decimals, updated_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except sqlite3.OperationalError:
        logger.warning(
            "Could not persist pair_metadata (table may not exist yet)",
            exc_info=True,
        )


__all__ = [
    "PairMetadataCache",
]
