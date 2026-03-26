"""Tests for research.labels — forward-looking label computation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.labels import compute_labels


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ohlcv(close_prices: list[float]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame from a list of close prices."""
    n = len(close_prices)
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="h"),
        "open": close_prices,
        "high": close_prices,
        "low": close_prices,
        "close": close_prices,
        "volume": [1.0] * n,
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReturnBpsCorrectness:
    """return_bps should equal 10000 * (close[t+h] - close[t]) / close[t]."""

    def test_return_bps_6h(self) -> None:
        # 20 rows with a known pattern
        prices = [100.0 + i * 5.0 for i in range(20)]  # 100, 105, ..., 195
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6])

        # Row 0: close[0]=100, close[6]=130 -> bps = 10000*(130-100)/100 = 3000
        assert result["return_bps_6h"].iloc[0] == pytest.approx(3000.0)

        # Row 5: close[5]=125, close[11]=155 -> bps = 10000*(155-125)/125 = 2400
        assert result["return_bps_6h"].iloc[5] == pytest.approx(2400.0)

    def test_return_bps_known_simple(self) -> None:
        """close[0]=100, close[6]=110 => return_bps_6h[0] = 1000.0"""
        prices = [100.0, 101.0, 102.0, 103.0, 105.0, 107.0, 110.0,
                  112.0, 114.0, 115.0, 116.0, 118.0, 120.0,
                  121.0, 122.0, 123.0, 124.0, 125.0, 126.0, 127.0]
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6])
        assert result["return_bps_6h"].iloc[0] == pytest.approx(1000.0)


class TestReturnSign:
    """return_sign should match the direction of return_bps."""

    def test_positive_return(self) -> None:
        prices = [100.0] * 6 + [110.0] + [100.0] * 13
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6])
        # Row 0: close goes from 100 -> 110 -> positive
        assert result["return_sign_6h"].iloc[0] == 1.0

    def test_negative_return(self) -> None:
        prices = [110.0] * 6 + [100.0] + [110.0] * 13
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6])
        # Row 0: close goes from 110 -> 100 -> negative
        assert result["return_sign_6h"].iloc[0] == -1.0

    def test_zero_return(self) -> None:
        prices = [100.0] * 20
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6])
        assert result["return_sign_6h"].iloc[0] == 0.0


class TestNanAtBoundaries:
    """The last h rows must be NaN for return columns of horizon h."""

    def test_last_h_rows_are_nan(self) -> None:
        prices = [100.0 + i for i in range(20)]
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6, 12])

        # Last 6 rows should be NaN for 6h columns
        assert result["return_bps_6h"].iloc[-6:].isna().all()
        assert result["return_sign_6h"].iloc[-6:].isna().all()

        # Last 12 rows should be NaN for 12h columns
        assert result["return_bps_12h"].iloc[-12:].isna().all()
        assert result["return_sign_12h"].iloc[-12:].isna().all()

    def test_non_boundary_rows_not_nan(self) -> None:
        prices = [100.0 + i for i in range(20)]
        df = _make_ohlcv(prices)
        result = compute_labels(df, horizons=[6])

        # First 14 rows (20 - 6) should NOT be NaN
        assert result["return_bps_6h"].iloc[:14].notna().all()
        assert result["return_sign_6h"].iloc[:14].notna().all()


class TestRegimeLabelCategories:
    """regime_label values must be in {'low', 'medium', 'high'} or NaN."""

    def test_valid_categories(self) -> None:
        # Need enough rows for vol_lookback (default 24) to produce non-NaN
        np.random.seed(42)
        n = 100
        prices = 100.0 + np.cumsum(np.random.randn(n) * 0.5)
        prices = np.maximum(prices, 1.0)  # keep positive
        df = _make_ohlcv(prices.tolist())
        result = compute_labels(df)

        valid_labels = {"low", "medium", "high"}
        non_null = result["regime_label"].dropna().unique()
        for label in non_null:
            assert label in valid_labels, f"Unexpected regime_label: {label}"

    def test_early_rows_can_be_nan(self) -> None:
        prices = [100.0 + i * 0.1 for i in range(50)]
        df = _make_ohlcv(prices)
        result = compute_labels(df, vol_lookback=24)

        # First 24 rows need the lookback window so regime_label[0] should be NaN
        assert pd.isna(result["regime_label"].iloc[0])


class TestDefaultHorizons:
    """Default horizons should be [6, 12]."""

    def test_default_columns_present(self) -> None:
        prices = [100.0 + i for i in range(30)]
        df = _make_ohlcv(prices)
        result = compute_labels(df)

        assert "return_bps_6h" in result.columns
        assert "return_sign_6h" in result.columns
        assert "return_bps_12h" in result.columns
        assert "return_sign_12h" in result.columns
        assert "regime_label" in result.columns


class TestDeterministic:
    """Same input must produce identical output."""

    def test_same_input_same_output(self) -> None:
        np.random.seed(99)
        prices = (100.0 + np.cumsum(np.random.randn(60) * 0.3)).tolist()
        df = _make_ohlcv(prices)

        result1 = compute_labels(df, horizons=[6, 12])
        result2 = compute_labels(df, horizons=[6, 12])

        pd.testing.assert_frame_equal(result1, result2)
