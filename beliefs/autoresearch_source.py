from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd

from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    MarketRegime,
)

OHLCV_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
TOTAL_SIGNALS: Final[int] = 6
MOMENTUM_12H_LOOKBACK: Final[int] = 12
MOMENTUM_6H_LOOKBACK: Final[int] = 6
EMA_FAST_SPAN: Final[int] = 7
EMA_SLOW_SPAN: Final[int] = 26
RSI_PERIOD: Final[int] = 8
MACD_FAST_SPAN: Final[int] = 14
MACD_SLOW_SPAN: Final[int] = 23
MACD_SIGNAL_SPAN: Final[int] = 9
BOLLINGER_WINDOW: Final[int] = 20
BOLLINGER_PERCENTILE: Final[float] = 85.0
MIN_REQUIRED_BARS: Final[int] = 40


class AutoResearchError(ValueError):
    """Base exception for auto-research signal evaluation failures."""


class AutoResearchInputError(AutoResearchError):
    """Raised when OHLCV input data is missing or malformed."""


class MissingOHLCVColumnsError(AutoResearchInputError):
    """Raised when required OHLCV columns are absent."""

    def __init__(self, missing_columns: tuple[str, ...]) -> None:
        self.missing_columns = missing_columns
        formatted = ", ".join(missing_columns)
        super().__init__(f"OHLCV bars are missing required columns: {formatted}")


class InsufficientBarDataError(AutoResearchInputError):
    """Raised when the provided bar series is too short for the indicator."""

    def __init__(self, minimum_bars: int, actual_bars: int) -> None:
        self.minimum_bars = minimum_bars
        self.actual_bars = actual_bars
        super().__init__(
            f"Auto-research requires at least {minimum_bars} bars; got {actual_bars}."
        )


@dataclass(frozen=True, slots=True)
class AutoResearchSignals:
    momentum_12h: bool
    momentum_6h: bool
    ema_crossover: bool
    rsi_above_50: bool
    macd_histogram_positive: bool
    bollinger_width_compressed: bool

    def as_tuple(self) -> tuple[bool, ...]:
        return (
            self.momentum_12h,
            self.momentum_6h,
            self.ema_crossover,
            self.rsi_above_50,
            self.macd_histogram_positive,
            self.bollinger_width_compressed,
        )

    @property
    def bullish_count(self) -> int:
        return sum(1 for signal in self.as_tuple() if signal)

    @property
    def bearish_count(self) -> int:
        return TOTAL_SIGNALS - self.bullish_count

    @property
    def agreement_count(self) -> int:
        return max(self.bullish_count, self.bearish_count)


class AutoResearchSource:
    """Pure-Python adapter for the 6-signal auto-research ensemble."""

    def __init__(self, min_bars: int = MIN_REQUIRED_BARS) -> None:
        if min_bars < BOLLINGER_WINDOW:
            raise AutoResearchInputError(
                f"min_bars must be at least {BOLLINGER_WINDOW}; got {min_bars}."
            )
        self.min_bars = min_bars

    def analyze(self, pair: str, bars: pd.DataFrame) -> BeliefSnapshot:
        signals = self.compute_signals(bars)
        return self.build_snapshot(pair=pair, signals=signals)

    def compute_signals(self, bars: pd.DataFrame) -> AutoResearchSignals:
        close = self._extract_close_series(bars, minimum_bars=self.min_bars)
        return AutoResearchSignals(
            momentum_12h=self.signal_12h_momentum(close),
            momentum_6h=self.signal_6h_momentum(close),
            ema_crossover=self.signal_ema_crossover(close),
            rsi_above_50=self.signal_rsi_above_50(close),
            macd_histogram_positive=self.signal_macd_histogram_positive(close),
            bollinger_width_compressed=self.signal_bollinger_width_compressed(close),
        )

    def build_snapshot(
        self,
        pair: str,
        signals: AutoResearchSignals,
    ) -> BeliefSnapshot:
        bullish_count = signals.bullish_count
        bearish_count = signals.bearish_count

        if bullish_count >= 4:
            direction = BeliefDirection.BULLISH
        elif bearish_count >= 4:
            direction = BeliefDirection.BEARISH
        else:
            direction = BeliefDirection.NEUTRAL

        confidence = round(signals.agreement_count / TOTAL_SIGNALS, 2)
        regime = (
            MarketRegime.RANGING
            if signals.bollinger_width_compressed
            else MarketRegime.TRENDING
        )

        return BeliefSnapshot(
            pair=pair,
            direction=direction,
            confidence=confidence,
            regime=regime,
            sources=(BeliefSource.AUTORESEARCH,),
        )

    def signal_12h_momentum(self, close: pd.Series) -> bool:
        return self._momentum_signal(close, lookback=MOMENTUM_12H_LOOKBACK)

    def signal_6h_momentum(self, close: pd.Series) -> bool:
        return self._momentum_signal(close, lookback=MOMENTUM_6H_LOOKBACK)

    def signal_ema_crossover(self, close: pd.Series) -> bool:
        series = self._coerce_close_series(close, minimum_bars=EMA_SLOW_SPAN)
        ema_fast = series.ewm(span=EMA_FAST_SPAN, adjust=False).mean()
        ema_slow = series.ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
        return bool(ema_fast.iloc[-1] > ema_slow.iloc[-1])

    def signal_rsi_above_50(self, close: pd.Series) -> bool:
        series = self._coerce_close_series(close, minimum_bars=RSI_PERIOD + 1)
        delta = series.diff()
        gains = delta.clip(lower=0.0)
        losses = -delta.clip(upper=0.0)
        avg_gain = float(gains.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean().iloc[-1])
        avg_loss = float(losses.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean().iloc[-1])

        if avg_loss == 0.0:
            rsi = 100.0 if avg_gain > 0.0 else 50.0
        else:
            relative_strength = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + relative_strength))

        return bool(rsi > 50.0)

    def signal_macd_histogram_positive(self, close: pd.Series) -> bool:
        series = self._coerce_close_series(close, minimum_bars=MACD_SLOW_SPAN)
        ema_fast = series.ewm(span=MACD_FAST_SPAN, adjust=False).mean()
        ema_slow = series.ewm(span=MACD_SLOW_SPAN, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=MACD_SIGNAL_SPAN, adjust=False).mean()
        histogram = macd_line - signal_line
        return bool(histogram.iloc[-1] > 0.0)

    def signal_bollinger_width_compressed(self, close: pd.Series) -> bool:
        series = self._coerce_close_series(close, minimum_bars=BOLLINGER_WINDOW)
        rolling_mean = series.rolling(window=BOLLINGER_WINDOW, min_periods=BOLLINGER_WINDOW).mean()
        rolling_std = series.rolling(window=BOLLINGER_WINDOW, min_periods=BOLLINGER_WINDOW).std(ddof=0)
        width = (4.0 * rolling_std) / rolling_mean.replace(0.0, np.nan)
        width = width.dropna()

        if width.empty:
            raise InsufficientBarDataError(BOLLINGER_WINDOW, len(series))

        threshold = float(np.percentile(width.to_numpy(dtype=float), BOLLINGER_PERCENTILE))
        return bool(float(width.iloc[-1]) < threshold)

    def _momentum_signal(self, close: pd.Series, lookback: int) -> bool:
        series = self._coerce_close_series(close, minimum_bars=lookback + 1)
        return bool(series.iloc[-1] > series.iloc[-(lookback + 1)])

    def _extract_close_series(
        self,
        bars: pd.DataFrame,
        minimum_bars: int,
    ) -> pd.Series:
        if not isinstance(bars, pd.DataFrame):
            raise AutoResearchInputError("bars must be a pandas DataFrame containing OHLCV data.")

        missing_columns = tuple(
            column for column in OHLCV_COLUMNS if column not in bars.columns
        )
        if missing_columns:
            raise MissingOHLCVColumnsError(missing_columns)

        return self._coerce_close_series(bars["close"], minimum_bars=minimum_bars)

    def _coerce_close_series(
        self,
        close: pd.Series,
        minimum_bars: int,
    ) -> pd.Series:
        series = pd.to_numeric(close, errors="coerce").astype(float).reset_index(drop=True)

        if len(series) < minimum_bars:
            raise InsufficientBarDataError(minimum_bars, len(series))
        if series.isna().any():
            raise AutoResearchInputError("close prices must be numeric and non-null.")
        if (series <= 0.0).any():
            raise AutoResearchInputError("close prices must be positive.")

        return series


__all__ = [
    "AutoResearchError",
    "AutoResearchInputError",
    "AutoResearchSignals",
    "AutoResearchSource",
    "InsufficientBarDataError",
    "MissingOHLCVColumnsError",
    "OHLCV_COLUMNS",
]
