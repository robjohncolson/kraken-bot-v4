"""Fetch OHLCV candle data from Kraken's public REST API."""

from __future__ import annotations

import logging
import time
from decimal import Decimal

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

# OHLCV cache: avoids redundant HTTP calls when multiple roots scan the same pair
_ohlcv_cache: dict[str, tuple[float, "pd.DataFrame"]] = {}
_OHLCV_CACHE_TTL_SEC = 300  # 5 minutes — matches plan_cycle interval

KRAKEN_OHLCV_URL = "https://api.kraken.com/0/public/OHLC"

# Map our normalized pairs to Kraken API pair names
_PAIR_MAP: dict[str, str] = {
    "DOGE/USD": "XDGUSD",
    "BTC/USD": "XXBTZUSD",
    "ETH/USD": "XETHZUSD",
    "XRP/USD": "XXRPZUSD",
    "SOL/USD": "SOLUSD",
    "SUI/USD": "SUIUSD",
}


class OHLCVFetchError(Exception):
    """Raised when OHLCV data cannot be fetched from Kraken."""


def kraken_pair_name(normalized_pair: str) -> str:
    """Convert our normalized pair to a Kraken API pair name."""
    name = _PAIR_MAP.get(normalized_pair)
    if name is None:
        # Fallback: strip slash
        name = normalized_pair.replace("/", "")
    return name


def fetch_ohlcv(
    pair: str,
    interval: int = 60,
    count: int = 50,
    *,
    timeout: float = 15.0,
) -> pd.DataFrame:
    """Fetch OHLCV candles from Kraken public API.

    Args:
        pair: Normalized pair (e.g. "DOGE/USD")
        interval: Candle interval in minutes (default 60 = 1 hour)
        count: Minimum number of candles desired (Kraken returns up to 720)
        timeout: HTTP timeout in seconds

    Returns:
        DataFrame with columns: open, high, low, close, volume
    """
    cache_key = f"{pair}:{interval}"
    now = time.monotonic()
    cached = _ohlcv_cache.get(cache_key)
    if cached is not None and now < cached[0] and len(cached[1]) >= count:
        return cached[1]

    kraken_pair = kraken_pair_name(pair)
    params = {"pair": kraken_pair, "interval": interval}

    try:
        response = httpx.get(KRAKEN_OHLCV_URL, params=params, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OHLCVFetchError(f"Failed to fetch OHLCV for {pair}: {exc}") from exc

    if data.get("error"):
        raise OHLCVFetchError(f"Kraken API error for {pair}: {data['error']}")

    result = data.get("result", {})
    # Result keys are the Kraken pair name — find the candle array
    candles = None
    for key, value in result.items():
        if key != "last" and isinstance(value, list):
            candles = value
            break

    if not candles:
        raise OHLCVFetchError(f"No candle data returned for {pair}")

    # Kraken OHLC format: [time, open, high, low, close, vwap, volume, count]
    rows = []
    for candle in candles:
        rows.append({
            "open": Decimal(candle[1]),
            "high": Decimal(candle[2]),
            "low": Decimal(candle[3]),
            "close": Decimal(candle[4]),
            "volume": Decimal(candle[6]),
        })

    df = pd.DataFrame(rows)
    _ohlcv_cache[cache_key] = (time.monotonic() + _OHLCV_CACHE_TTL_SEC, df)
    if len(df) < count:
        logger.warning(
            "OHLCV for %s: got %d candles, wanted %d", pair, len(df), count,
        )
    return df


__all__ = [
    "OHLCVFetchError",
    "fetch_ohlcv",
    "kraken_pair_name",
]
