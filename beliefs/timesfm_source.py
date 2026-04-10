from __future__ import annotations

import logging
from typing import Final

import numpy as np
import pandas as pd

from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    MarketRegime,
)

logger = logging.getLogger(__name__)

DEFAULT_HORIZON: Final[int] = 24
DEFAULT_CONTEXT_LENGTH: Final[int] = 512
MIN_REQUIRED_BARS: Final[int] = 40
DIRECTION_THRESHOLD: Final[float] = 0.005  # 0.5% move to signal direction
WIDE_SPREAD_THRESHOLD: Final[float] = 0.02  # 2% quantile spread = trending

# Quantile indices in TimesFM output (10 quantiles: 10th..90th percentile)
Q10_INDEX: Final[int] = 1  # 10th percentile (bearish bound)
Q90_INDEX: Final[int] = 9  # 90th percentile (bullish bound)


class TimesFMSourceError(ValueError):
    """Base exception for TimesFM source failures."""


class InsufficientBarDataError(TimesFMSourceError):
    """Raised when the provided bar series is too short."""

    def __init__(self, minimum_bars: int, actual_bars: int) -> None:
        self.minimum_bars = minimum_bars
        self.actual_bars = actual_bars
        super().__init__(
            f"TimesFM requires at least {minimum_bars} bars; got {actual_bars}."
        )


class TimesFMSource:
    """Belief source using Google TimesFM 2.5 for close-price forecasting."""

    def __init__(
        self,
        horizon: int = DEFAULT_HORIZON,
        context_length: int = DEFAULT_CONTEXT_LENGTH,
        min_bars: int = MIN_REQUIRED_BARS,
    ) -> None:
        self.horizon = horizon
        self.context_length = context_length
        self.min_bars = min_bars
        self._model = None

    def _ensure_model(self) -> None:
        """Lazy-load TimesFM model on first use."""
        if self._model is not None:
            return
        import timesfm  # noqa: PLC0415

        logger.info("Loading TimesFM 2.5 model (first use)...")
        self._model = timesfm.TimesFM_2p5_200M_torch.from_pretrained(
            "google/timesfm-2.5-200m-pytorch",
            torch_compile=False,
        )
        self._model.compile(timesfm.ForecastConfig(
            max_context=self.context_length,
            max_horizon=self.horizon,
            normalize_inputs=True,
            use_continuous_quantile_head=True,
            force_flip_invariance=False,
            infer_is_positive=True,
        ))
        logger.info("TimesFM model loaded")

    def analyze(self, pair: str, bars: pd.DataFrame) -> BeliefSnapshot:
        """Generate a belief from close-price forecast."""
        if len(bars) < self.min_bars:
            raise InsufficientBarDataError(self.min_bars, len(bars))

        self._ensure_model()

        close = bars["close"].values.astype(np.float32)
        context = close[-self.context_length:]

        point_forecast, quantile_forecast = self._model.forecast(
            horizon=self.horizon,
            inputs=[context],
        )

        current_price = float(close[-1])
        predicted_price = float(point_forecast[0, -1])
        p10 = float(quantile_forecast[0, -1, Q10_INDEX])
        p90 = float(quantile_forecast[0, -1, Q90_INDEX])

        direction = _compute_direction(current_price, predicted_price)
        confidence = _compute_confidence(current_price, p10, p90, direction)
        spread = (p90 - p10) / current_price if current_price > 0 else 0.0
        regime = MarketRegime.TRENDING if spread > WIDE_SPREAD_THRESHOLD else MarketRegime.RANGING

        return BeliefSnapshot(
            pair=pair,
            direction=direction,
            confidence=confidence,
            regime=regime,
            sources=(BeliefSource.TIMESFM,),
        )


def _compute_direction(current: float, predicted: float) -> BeliefDirection:
    """Map predicted price to a direction."""
    if current <= 0:
        return BeliefDirection.NEUTRAL
    pct_change = (predicted - current) / current
    if pct_change > DIRECTION_THRESHOLD:
        return BeliefDirection.BULLISH
    if pct_change < -DIRECTION_THRESHOLD:
        return BeliefDirection.BEARISH
    return BeliefDirection.NEUTRAL


def _compute_confidence(
    current: float,
    p10: float,
    p90: float,
    direction: BeliefDirection,
) -> float:
    """Derive confidence from quantile spread relative to direction.

    High confidence bullish: even the 10th percentile is above current.
    High confidence bearish: even the 90th percentile is below current.
    """
    if direction is BeliefDirection.NEUTRAL or current <= 0:
        return 0.0
    if direction is BeliefDirection.BULLISH:
        raw = (p10 - current) / current * 10 + 0.5
    else:
        raw = (current - p90) / current * 10 + 0.5
    return round(min(1.0, max(0.3, raw)), 2)
