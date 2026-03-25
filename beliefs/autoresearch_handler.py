"""Belief refresh handler backed by AutoResearchSource + Kraken OHLCV."""

from __future__ import annotations

import logging

from beliefs.autoresearch_source import AutoResearchSource
from core.types import BeliefSnapshot
from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv
from scheduler import BeliefRefreshRequest

logger = logging.getLogger(__name__)

_source = AutoResearchSource()


def autoresearch_belief_handler(
    request: BeliefRefreshRequest,
) -> BeliefSnapshot | None:
    """Fetch OHLCV from Kraken, run AutoResearch, return a BeliefSnapshot."""
    pair = request.pair
    try:
        bars = fetch_ohlcv(pair, interval=60, count=50)
    except OHLCVFetchError as exc:
        logger.warning("Belief refresh for %s: OHLCV fetch failed: %s", pair, exc)
        return None

    if len(bars) < _source.min_bars:
        logger.warning(
            "Belief refresh for %s: only %d bars (need %d)",
            pair, len(bars), _source.min_bars,
        )
        return None

    try:
        snapshot = _source.analyze(pair, bars)
    except Exception as exc:
        logger.warning("Belief refresh for %s: analysis failed: %s", pair, exc)
        return None

    logger.info(
        "Belief refresh for %s: direction=%s confidence=%.2f regime=%s",
        pair, snapshot.direction.value, snapshot.confidence, snapshot.regime.value,
    )
    return snapshot


def generate_belief(pair: str) -> BeliefSnapshot | None:
    """Standalone belief generation for a pair (used by periodic poll)."""
    try:
        bars = fetch_ohlcv(pair, interval=60, count=50)
    except OHLCVFetchError as exc:
        logger.warning("Belief generation for %s: OHLCV fetch failed: %s", pair, exc)
        return None

    if len(bars) < _source.min_bars:
        logger.warning(
            "Belief generation for %s: only %d bars (need %d)",
            pair, len(bars), _source.min_bars,
        )
        return None

    try:
        snapshot = _source.analyze(pair, bars)
    except Exception as exc:
        logger.warning("Belief generation for %s: analysis failed: %s", pair, exc)
        return None

    logger.info(
        "Belief generated for %s: direction=%s confidence=%.2f regime=%s",
        pair, snapshot.direction.value, snapshot.confidence, snapshot.regime.value,
    )
    return snapshot


__all__ = [
    "autoresearch_belief_handler",
    "generate_belief",
]
