"""Fetch historical OHLCV data from CryptoCompare (exchange-specific).

Used as a research data source when Kraken's native REST OHLC endpoint
(720-candle limit) is insufficient. Always pins ``e=Kraken`` to get
Kraken-specific candles, NOT the CCCAGG aggregate.

This module is intentionally separate from ``ohlcv_history.py`` (Kraken-native)
and should only be used for research dataset export, not live trading signals.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

CRYPTOCOMPARE_HISTOHOUR_URL = "https://min-api.cryptocompare.com/data/v2/histohour"
MAX_CANDLES_PER_REQUEST = 2000


class CryptoCompareError(Exception):
    """Raised when CryptoCompare API returns an error."""


def _cc_symbol(pair: str) -> tuple[str, str]:
    """Convert normalised pair like 'DOGE/USD' to (fsym, tsym)."""
    parts = pair.split("/")
    if len(parts) != 2:
        raise CryptoCompareError(f"Invalid pair format: {pair!r}")
    return parts[0], parts[1]


def fetch_ohlcv_cryptocompare(
    pair: str,
    interval: int = 60,
    since: int | None = None,
    until: int | None = None,
    exchange: str = "Kraken",
    timeout: float = 15.0,
) -> pd.DataFrame:
    """Fetch hourly OHLCV history from CryptoCompare for a specific exchange.

    Paginates backward using ``toTs`` until *since* is reached or no
    more data is available.

    Args:
        pair: Normalised pair (e.g. ``"DOGE/USD"``).
        interval: Candle interval in minutes. Only 60 is supported
            (CryptoCompare ``histohour``).
        since: Start timestamp (Unix epoch seconds).  Omit for max history.
        until: End timestamp (Unix epoch seconds).  Omit for latest.
        exchange: Exchange name pinned in every request (default ``"Kraken"``).
        timeout: HTTP request timeout in seconds.

    Returns:
        DataFrame with columns: ``timestamp``, ``open``, ``high``,
        ``low``, ``close``, ``volume`` (all :class:`~decimal.Decimal`).
    """
    if interval != 60:
        raise CryptoCompareError(
            f"Only 60-minute interval is supported, got {interval}"
        )

    fsym, tsym = _cc_symbol(pair)
    all_rows: list[dict] = []
    cursor_ts = until  # None means "latest"

    while True:
        params: dict[str, str | int] = {
            "fsym": fsym,
            "tsym": tsym,
            "limit": MAX_CANDLES_PER_REQUEST,
            "e": exchange,
        }
        if cursor_ts is not None:
            params["toTs"] = cursor_ts

        try:
            resp = httpx.get(
                CRYPTOCOMPARE_HISTOHOUR_URL, params=params, timeout=timeout
            )
            resp.raise_for_status()
            body = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CryptoCompareError(
                f"CryptoCompare request failed for {pair}: {exc}"
            ) from exc

        if body.get("Response") != "Success":
            raise CryptoCompareError(
                f"CryptoCompare error for {pair}: {body.get('Message', body)}"
            )

        candles = body.get("Data", {}).get("Data", [])
        if not candles:
            break

        page_rows: list[dict] = []
        for c in candles:
            ts = int(c["time"])
            if since is not None and ts < since:
                continue
            page_rows.append({
                "timestamp": ts,
                "open": Decimal(str(c["open"])),
                "high": Decimal(str(c["high"])),
                "low": Decimal(str(c["low"])),
                "close": Decimal(str(c["close"])),
                "volume": Decimal(str(c["volumefrom"])),
            })

        all_rows.extend(page_rows)

        earliest_ts = int(candles[0]["time"])
        if since is not None and earliest_ts <= since:
            break
        if cursor_ts is not None and earliest_ts >= cursor_ts:
            break  # no progress

        cursor_ts = earliest_ts

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["timestamp"], keep="first")
        df = df.sort_values("timestamp").reset_index(drop=True)

    if df.empty:
        logger.warning("CryptoCompare OHLCV for %s: no candles returned", pair)
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )

    logger.info(
        "CryptoCompare OHLCV for %s (e=%s): fetched %d candles (%d -> %d)",
        pair, exchange, len(df), df["timestamp"].iloc[0], df["timestamp"].iloc[-1],
    )
    return df


__all__ = [
    "CryptoCompareError",
    "fetch_ohlcv_cryptocompare",
]
