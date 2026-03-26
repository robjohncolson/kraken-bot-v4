"""Fetch historical OHLCV candle data from Kraken with pagination."""

from __future__ import annotations

import logging
from decimal import Decimal

import httpx
import pandas as pd

from exchange.ohlcv import KRAKEN_OHLCV_URL, kraken_pair_name

logger = logging.getLogger(__name__)


class OHLCVHistoryError(Exception):
    """Raised when historical OHLCV data cannot be fetched from Kraken."""


def fetch_ohlcv_history(
    pair: str,
    interval: int,
    since: int,
    until: int | None = None,
    timeout: float = 15.0,
) -> pd.DataFrame:
    """Fetch OHLCV candle history from Kraken with automatic pagination.

    Kraken returns up to 720 candles per request. This function paginates
    using the ``last`` field in each response until either *until* is
    reached or no more data is returned.

    Args:
        pair: Normalized pair (e.g. ``"DOGE/USD"``).
        interval: Candle interval in minutes (1, 5, 15, 30, 60, 240, 1440, 10080, 21600).
        since: Start timestamp (Unix epoch **seconds**).
        until: Optional end timestamp (Unix epoch **seconds**). If ``None``,
            fetches all available data from *since* onward.
        timeout: HTTP request timeout in seconds.

    Returns:
        DataFrame with columns: ``timestamp`` (int), ``open``, ``high``,
        ``low``, ``close``, ``volume`` (all :class:`~decimal.Decimal`).

    Raises:
        OHLCVHistoryError: On HTTP errors, Kraken API errors, or missing data.
    """
    kraken_pair = kraken_pair_name(pair)
    all_rows: list[dict] = []
    cursor = since

    while True:
        params: dict[str, int | str] = {
            "pair": kraken_pair,
            "interval": interval,
            "since": cursor,
        }

        try:
            response = httpx.get(KRAKEN_OHLCV_URL, params=params, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OHLCVHistoryError(
                f"Failed to fetch OHLCV history for {pair}: {exc}"
            ) from exc

        errors = data.get("error", [])
        if errors:
            raise OHLCVHistoryError(
                f"Kraken API error for {pair}: {errors}"
            )

        result = data.get("result", {})

        # Find the candle array (key is the Kraken pair name, not "last")
        candles: list | None = None
        for key, value in result.items():
            if key != "last" and isinstance(value, list):
                candles = value
                break

        if candles is None:
            raise OHLCVHistoryError(f"No candle data returned for {pair}")

        if not candles:
            # Empty list — no more data available
            break

        for candle in candles:
            ts = int(candle[0])
            if until is not None and ts >= until:
                # We've reached the end boundary
                break
            all_rows.append({
                "timestamp": ts,
                "open": Decimal(candle[1]),
                "high": Decimal(candle[2]),
                "low": Decimal(candle[3]),
                "close": Decimal(candle[4]),
                "volume": Decimal(candle[6]),
            })
        else:
            # Inner loop didn't break — check pagination
            next_cursor = result.get("last")
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
            continue

        # Inner loop broke (hit `until`) — stop paginating
        break

    df = pd.DataFrame(all_rows)
    if df.empty:
        logger.warning("OHLCV history for %s: no candles returned", pair)
        return pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )

    logger.info(
        "OHLCV history for %s: fetched %d candles (%d -> %d)",
        pair,
        len(df),
        df["timestamp"].iloc[0],
        df["timestamp"].iloc[-1],
    )
    return df


__all__ = [
    "OHLCVHistoryError",
    "fetch_ohlcv_history",
]
