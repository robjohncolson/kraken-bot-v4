from __future__ import annotations

import pandas as pd
import pytest

from trading.duration_estimator import (
    InsufficientBarDataError,
    MissingOHLCVColumnsError,
    estimate_bear_duration,
)


def _bars_from_close(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": close * 1.01,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1000.0,
            }
            for close in closes
        ]
    )


def test_estimate_bear_duration_returns_24h_for_bearish_non_oversold_setup() -> None:
    closes = [
        100, 99, 100, 98, 99, 97, 98, 96, 97, 95,
        96, 94, 95, 93, 94, 92, 93, 91, 92, 90,
        91, 89, 90, 88, 89, 87, 88, 86, 87, 85,
        86, 84, 85, 83, 84, 82, 83, 81, 82, 80,
    ]

    estimate = estimate_bear_duration(_bars_from_close(closes))

    assert estimate.estimated_bear_hours == 24
    assert estimate.confidence == 1.0
    assert estimate.macd_bearish is True
    assert estimate.rsi_bearish is True
    assert estimate.ema_bearish is True


def test_estimate_bear_duration_returns_12h_for_mixed_signals() -> None:
    closes = [
        120.0, 119.6, 119.2, 118.8, 118.4, 118.0, 117.6, 117.2, 116.8, 116.4,
        116.0, 115.6, 115.2, 114.8, 114.4, 114.0, 113.6, 113.2, 112.8, 112.4,
        111.0, 105.0, 99.0, 93.0, 87.0, 81.0, 75.0, 78.0, 81.0, 84.0,
        83.0, 82.0, 81.0, 80.0, 79.0, 78.0, 77.0, 76.0, 75.0, 74.0,
    ]

    estimate = estimate_bear_duration(_bars_from_close(closes))

    assert estimate.estimated_bear_hours == 12
    assert estimate.confidence == 0.67
    assert estimate.macd_bearish is False
    assert estimate.rsi_bearish is True
    assert estimate.ema_bearish is True


def test_estimate_bear_duration_extends_bucket_when_macd_slope_keeps_falling() -> None:
    closes = (
        [100 + (i * 0.3) for i in range(20)]
        + [106, 105.8, 105.6, 105.4, 105.2, 105.0, 104.8, 104.6, 104.4, 104.2,
           104.0, 103.8, 103.6, 103.4, 103.2]
        + [106.0, 105.8, 105.6, 105.4, 104.9]
    )

    estimate = estimate_bear_duration(_bars_from_close(closes))

    assert estimate.estimated_bear_hours == 24
    assert estimate.confidence == 0.67
    assert estimate.macd_bearish is True
    assert estimate.rsi_bearish is True
    assert estimate.ema_bearish is False


def test_estimate_bear_duration_reduces_bucket_when_rsi_is_oversold() -> None:
    closes = [100 + (i * 0.5) for i in range(20)] + [
        110, 109.8, 109.5, 109.1, 108.8, 108.4, 108.1, 107.7, 107.4, 107.0,
        106.7, 106.4, 106.1, 105.8, 105.5, 105.2, 104.9, 104.6, 104.3, 104.0,
    ]

    estimate = estimate_bear_duration(_bars_from_close(closes))

    assert estimate.estimated_bear_hours == 12
    assert estimate.confidence == 1.0
    assert estimate.macd_bearish is True
    assert estimate.rsi_bearish is True
    assert estimate.ema_bearish is True


@pytest.mark.parametrize(
    ("bars", "error_type"),
    [
        (
            pd.DataFrame(
                {
                    "open": [100.0] * 26,
                    "high": [101.0] * 26,
                    "low": [99.0] * 26,
                    "close": [100.0] * 26,
                }
            ),
            MissingOHLCVColumnsError,
        ),
        (_bars_from_close([100.0 + (i * 0.1) for i in range(25)]), InsufficientBarDataError),
    ],
)
def test_estimate_bear_duration_rejects_invalid_input(
    bars: pd.DataFrame,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        estimate_bear_duration(bars)
