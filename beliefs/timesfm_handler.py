"""Belief refresh handler backed by Google TimesFM 2.5 close-price forecaster."""

from __future__ import annotations

import logging

from beliefs.timesfm_source import TimesFMSource
from core.types import BeliefSnapshot
from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv
from scheduler import BeliefRefreshRequest

logger = logging.getLogger(__name__)

# TimesFM needs more bars than technical ensemble for context window
OHLCV_BARS = 520  # ~21 days of 1h candles (default context=512 + some margin)


def make_timesfm_handler() -> callable:
    """Create a lazy-initialized TimesFM belief handler."""
    source = TimesFMSource()

    def handler(request: BeliefRefreshRequest) -> BeliefSnapshot | None:
        pair = request.pair
        try:
            bars = fetch_ohlcv(pair, interval=60, count=OHLCV_BARS)
        except OHLCVFetchError as exc:
            logger.warning("TimesFM belief for %s: OHLCV fetch failed: %s", pair, exc)
            return None

        if len(bars) < source.min_bars:
            logger.warning(
                "TimesFM belief for %s: only %d bars (need %d)",
                pair, len(bars), source.min_bars,
            )
            return None

        try:
            snapshot = source.analyze(pair, bars)
        except Exception as exc:
            logger.warning("TimesFM belief for %s: analysis failed: %s", pair, exc)
            return None

        logger.info(
            "TimesFM belief for %s: direction=%s confidence=%.2f regime=%s",
            pair, snapshot.direction.value, snapshot.confidence, snapshot.regime.value,
        )
        return snapshot

    return handler


__all__ = ["make_timesfm_handler"]
