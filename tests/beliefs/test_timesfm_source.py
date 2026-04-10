from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from beliefs.timesfm_source import (
    DEFAULT_CONTEXT_LENGTH,
    DEFAULT_HORIZON,
    InsufficientBarDataError,
    TimesFMSource,
    _compute_confidence,
    _compute_direction,
)
from core.types import BeliefDirection, BeliefSource, MarketRegime


def _make_bars(n: int, close_start: float = 100.0, close_end: float = 100.0) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame with linearly spaced close prices."""
    closes = np.linspace(close_start, close_end, n)
    return pd.DataFrame({
        "open": closes,
        "high": closes * 1.01,
        "low": closes * 0.99,
        "close": closes,
        "volume": np.ones(n) * 1000,
    })


def _mock_forecast(predicted_price: float, p10: float, p90: float, horizon: int = DEFAULT_HORIZON):
    """Return (point_forecast, quantile_forecast) arrays matching TimesFM shape."""
    point = np.zeros((1, horizon))
    point[0, -1] = predicted_price
    quantiles = np.zeros((1, horizon, 10))
    quantiles[0, -1, 1] = p10   # Q10
    quantiles[0, -1, 5] = predicted_price  # median
    quantiles[0, -1, 9] = p90  # Q90
    return point, quantiles


class TestComputeDirection:
    def test_bullish_above_threshold(self):
        assert _compute_direction(100.0, 101.0) is BeliefDirection.BULLISH

    def test_bearish_below_threshold(self):
        assert _compute_direction(100.0, 99.0) is BeliefDirection.BEARISH

    def test_neutral_within_threshold(self):
        assert _compute_direction(100.0, 100.3) is BeliefDirection.NEUTRAL

    def test_neutral_exact(self):
        assert _compute_direction(100.0, 100.0) is BeliefDirection.NEUTRAL

    def test_zero_current_returns_neutral(self):
        assert _compute_direction(0.0, 50.0) is BeliefDirection.NEUTRAL


class TestComputeConfidence:
    def test_bullish_p10_above_current(self):
        conf = _compute_confidence(100.0, 102.0, 108.0, BeliefDirection.BULLISH)
        assert 0.3 <= conf <= 1.0

    def test_bearish_p90_below_current(self):
        conf = _compute_confidence(100.0, 88.0, 95.0, BeliefDirection.BEARISH)
        assert 0.3 <= conf <= 1.0

    def test_neutral_returns_zero(self):
        assert _compute_confidence(100.0, 99.0, 101.0, BeliefDirection.NEUTRAL) == 0.0

    def test_confidence_clamped_at_min(self):
        conf = _compute_confidence(100.0, 99.0, 101.0, BeliefDirection.BULLISH)
        assert conf >= 0.3

    def test_confidence_clamped_at_max(self):
        conf = _compute_confidence(100.0, 120.0, 130.0, BeliefDirection.BULLISH)
        assert conf <= 1.0


class TestTimesFMSource:
    def test_insufficient_bars_raises(self):
        source = TimesFMSource()
        bars = _make_bars(10)
        with pytest.raises(InsufficientBarDataError):
            source.analyze("BTC/USD", bars)

    def test_model_lazy_loaded(self):
        source = TimesFMSource()
        assert source._model is None

    @patch("beliefs.timesfm_source.TimesFMSource._ensure_model")
    def test_analyze_bullish(self, mock_ensure):
        source = TimesFMSource()
        source._model = MagicMock()
        source._model.forecast.return_value = _mock_forecast(
            predicted_price=110.0, p10=105.0, p90=115.0,
        )
        bars = _make_bars(50, close_start=95.0, close_end=100.0)
        result = source.analyze("BTC/USD", bars)

        assert result.direction is BeliefDirection.BULLISH
        assert result.pair == "BTC/USD"
        assert BeliefSource.TIMESFM in result.sources
        assert 0.3 <= result.confidence <= 1.0

    @patch("beliefs.timesfm_source.TimesFMSource._ensure_model")
    def test_analyze_bearish(self, mock_ensure):
        source = TimesFMSource()
        source._model = MagicMock()
        source._model.forecast.return_value = _mock_forecast(
            predicted_price=90.0, p10=85.0, p90=95.0,
        )
        bars = _make_bars(50, close_start=105.0, close_end=100.0)
        result = source.analyze("ETH/USD", bars)

        assert result.direction is BeliefDirection.BEARISH
        assert result.pair == "ETH/USD"

    @patch("beliefs.timesfm_source.TimesFMSource._ensure_model")
    def test_analyze_neutral(self, mock_ensure):
        source = TimesFMSource()
        source._model = MagicMock()
        source._model.forecast.return_value = _mock_forecast(
            predicted_price=100.2, p10=99.0, p90=101.0,
        )
        bars = _make_bars(50, close_end=100.0)
        result = source.analyze("DOGE/USD", bars)

        assert result.direction is BeliefDirection.NEUTRAL
        assert result.confidence == 0.0

    @patch("beliefs.timesfm_source.TimesFMSource._ensure_model")
    def test_regime_trending_wide_spread(self, mock_ensure):
        source = TimesFMSource()
        source._model = MagicMock()
        # p90 - p10 = 5.0, spread = 5% > 2% threshold → TRENDING
        source._model.forecast.return_value = _mock_forecast(
            predicted_price=105.0, p10=100.0, p90=105.0,
        )
        bars = _make_bars(50, close_end=100.0)
        result = source.analyze("SOL/USD", bars)
        assert result.regime is MarketRegime.TRENDING

    @patch("beliefs.timesfm_source.TimesFMSource._ensure_model")
    def test_regime_ranging_tight_spread(self, mock_ensure):
        source = TimesFMSource()
        source._model = MagicMock()
        # p90 - p10 = 1.0, spread = 1% < 2% threshold → RANGING
        source._model.forecast.return_value = _mock_forecast(
            predicted_price=105.0, p10=104.5, p90=105.5,
        )
        bars = _make_bars(50, close_end=100.0)
        result = source.analyze("ADA/USD", bars)
        assert result.regime is MarketRegime.RANGING

    @patch("beliefs.timesfm_source.TimesFMSource._ensure_model")
    def test_context_window_capped(self, mock_ensure):
        source = TimesFMSource(context_length=100)
        source._model = MagicMock()
        source._model.forecast.return_value = _mock_forecast(
            predicted_price=110.0, p10=105.0, p90=115.0,
        )
        bars = _make_bars(200, close_end=100.0)
        source.analyze("BTC/USD", bars)

        # Verify only last 100 bars were passed to model
        call_args = source._model.forecast.call_args
        inputs = call_args.kwargs.get("inputs") or call_args[1].get("inputs")
        assert len(inputs[0]) == 100
