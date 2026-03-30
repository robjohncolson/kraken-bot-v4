from __future__ import annotations

from typing import Final

import pandas as pd

from core.types import DurationEstimate

OHLCV_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
MIN_REQUIRED_BARS: Final[int] = 26
EMA_FAST_SPAN: Final[int] = 12
EMA_SLOW_SPAN: Final[int] = 26
MACD_SIGNAL_SPAN: Final[int] = 9
RSI_PERIOD: Final[int] = 14
RSI_BEARISH_THRESHOLD: Final[float] = 50.0
RSI_OVERSOLD_THRESHOLD: Final[float] = 30.0
BEAR_HOUR_BUCKETS: Final[tuple[int, ...]] = (0, 6, 12, 24)


class DurationEstimatorError(ValueError):
    """Base exception for duration-estimation failures."""


class DurationEstimatorInputError(DurationEstimatorError):
    """Raised when OHLCV input data is missing or malformed."""


class MissingOHLCVColumnsError(DurationEstimatorInputError):
    """Raised when required OHLCV columns are absent."""

    def __init__(self, missing_columns: tuple[str, ...]) -> None:
        self.missing_columns = missing_columns
        formatted = ", ".join(missing_columns)
        super().__init__(f"OHLCV bars are missing required columns: {formatted}")


class InsufficientBarDataError(DurationEstimatorInputError):
    """Raised when the provided bar series is too short for the estimator."""

    def __init__(self, minimum_bars: int, actual_bars: int) -> None:
        self.minimum_bars = minimum_bars
        self.actual_bars = actual_bars
        super().__init__(
            f"Duration estimator requires at least {minimum_bars} bars; got {actual_bars}."
        )


def estimate_bear_duration(bars: pd.DataFrame) -> DurationEstimate:
    """Estimate how long a bearish phase is likely to remain actionable."""

    validated = _validate_ohlcv(bars)
    close = validated["close"]

    ema_fast = close.ewm(span=EMA_FAST_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
    ema_bearish = bool(ema_fast.iloc[-1] < ema_slow.iloc[-1])

    rsi = _compute_rsi(close)
    rsi_bearish = bool(rsi < RSI_BEARISH_THRESHOLD)

    histogram = _compute_macd_histogram(close)
    macd_bearish = bool(histogram.iloc[-1] < 0.0)

    bearish_count = sum((macd_bearish, rsi_bearish, ema_bearish))
    bucket_index = bearish_count

    if bucket_index > 0 and macd_bearish and _histogram_is_getting_more_negative(histogram):
        bucket_index = min(bucket_index + 1, len(BEAR_HOUR_BUCKETS) - 1)
    if bucket_index > 0 and rsi < RSI_OVERSOLD_THRESHOLD:
        bucket_index = max(bucket_index - 1, 0)

    return DurationEstimate(
        estimated_bear_hours=BEAR_HOUR_BUCKETS[bucket_index],
        confidence=round(bearish_count / 3, 2),
        macd_bearish=macd_bearish,
        rsi_bearish=rsi_bearish,
        ema_bearish=ema_bearish,
    )


def _validate_ohlcv(bars: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(bars, pd.DataFrame):
        raise DurationEstimatorInputError(
            "bars must be a pandas DataFrame containing OHLCV data."
        )

    missing_columns = tuple(column for column in OHLCV_COLUMNS if column not in bars.columns)
    if missing_columns:
        raise MissingOHLCVColumnsError(missing_columns)

    if len(bars) < MIN_REQUIRED_BARS:
        raise InsufficientBarDataError(MIN_REQUIRED_BARS, len(bars))

    validated = (
        bars.loc[:, OHLCV_COLUMNS]
        .apply(pd.to_numeric, errors="coerce")
        .astype(float)
        .reset_index(drop=True)
    )

    if validated.isna().any().any():
        raise DurationEstimatorInputError("OHLCV bars must be numeric and non-null.")
    if (validated[["open", "high", "low", "close"]] <= 0.0).any().any():
        raise DurationEstimatorInputError("OHLC prices must be positive.")
    if (validated["volume"] < 0.0).any():
        raise DurationEstimatorInputError("volume must be non-negative.")

    return validated


def _compute_macd_histogram(close: pd.Series) -> pd.Series:
    ema_fast = close.ewm(span=EMA_FAST_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL_SPAN, adjust=False).mean()
    return macd_line - signal_line


def _compute_rsi(close: pd.Series) -> float:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = float(gains.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean().iloc[-1])
    avg_loss = float(losses.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean().iloc[-1])

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0

    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _histogram_is_getting_more_negative(histogram: pd.Series) -> bool:
    return bool(histogram.iloc[-1] < histogram.iloc[-2] < 0.0)


__all__ = [
    "DurationEstimatorError",
    "DurationEstimatorInputError",
    "InsufficientBarDataError",
    "MIN_REQUIRED_BARS",
    "MissingOHLCVColumnsError",
    "OHLCV_COLUMNS",
    "estimate_bear_duration",
]
