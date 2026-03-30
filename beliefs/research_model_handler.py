"""Handler factory for artifact-driven research model beliefs.

Creates a ``BeliefRefreshHandler`` that loads a promoted artifact and
generates ``BeliefSnapshot`` objects from live OHLCV data.  Also
provides a shadow variant that logs predictions (including raw
``prob_up``) without enqueueing them to the reducer.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from beliefs.research_model_source import ArtifactLoadError, ResearchModelSource
from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv

if TYPE_CHECKING:
    from core.types import BeliefSnapshot

logger = logging.getLogger(__name__)


def make_research_model_handler(
    artifact_dir: Path,
) -> "BeliefRefreshHandler":
    """Create a handler that uses a promoted artifact for belief generation.

    The returned callable matches the ``BeliefRefreshHandler`` type alias::

        (BeliefRefreshRequest) -> BeliefSnapshot | None

    Raises ``ArtifactLoadError`` at construction time if the artifact
    is missing, malformed, or has incompatible schemas.
    """
    source = ResearchModelSource(artifact_dir)

    def handler(request: "BeliefRefreshRequest") -> "BeliefSnapshot | None":
        try:
            bars = fetch_ohlcv(request.pair, interval=60, count=50)
        except OHLCVFetchError:
            logger.warning(
                "research_model: OHLCV fetch failed for %s", request.pair,
            )
            return None

        if len(bars) < source.min_bars:
            logger.warning(
                "research_model: insufficient bars for %s (%d < %d)",
                request.pair, len(bars), source.min_bars,
            )
            return None

        return source.analyze(request.pair, bars)

    return handler


def make_shadow_handler(
    artifact_dir: Path,
) -> "ShadowBeliefHandler":
    """Create a shadow handler that logs predictions without enqueueing.

    Returns a callable that:
    1. Fetches OHLCV for the requested pair
    2. Runs the research model
    3. Logs direction, confidence, and raw ``prob_up`` as structured log
    4. Returns ``None`` (shadow handlers never produce enqueueable beliefs)

    The returned type is::

        (BeliefRefreshRequest) -> None
    """
    source = ResearchModelSource(artifact_dir)

    def handler(request: "BeliefRefreshRequest") -> None:
        try:
            bars = fetch_ohlcv(request.pair, interval=60, count=50)
        except OHLCVFetchError:
            logger.warning(
                "shadow_model: OHLCV fetch failed for %s", request.pair,
            )
            return

        if len(bars) < source.min_bars:
            return

        belief = source.analyze(request.pair, bars)
        prob_up = source.predict_raw(bars)

        if belief is not None:
            logger.info(
                "shadow_prediction: pair=%s direction=%s confidence=%.4f "
                "prob_up=%.4f artifact=%s",
                belief.pair,
                belief.direction.value,
                belief.confidence,
                prob_up if prob_up is not None else -1.0,
                source.artifact_id,
            )

    return handler


__all__ = [
    "make_research_model_handler",
    "make_shadow_handler",
]
