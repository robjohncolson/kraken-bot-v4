from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from beliefs.autoresearch_source import AutoResearchSignals, AutoResearchSource
from core.types import BeliefDirection, BeliefSnapshot, BeliefSource, MarketRegime


def make_bars(close_values: list[float] | np.ndarray) -> pd.DataFrame:
    close = pd.Series(close_values, dtype=float)
    spread = np.maximum(close * 0.01, 0.5)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + spread,
            "low": np.maximum(close - spread, 0.01),
            "close": close,
            "volume": np.full(len(close), 1_000.0, dtype=float),
        }
    )


def test_signal_12h_momentum_uses_latest_close_against_twelve_bars_ago() -> None:
    source = AutoResearchSource()

    bullish_close = pd.Series(np.linspace(100.0, 130.0, 20), dtype=float)
    bearish_close = pd.Series(np.linspace(130.0, 100.0, 20), dtype=float)

    assert source.signal_12h_momentum(bullish_close) is True
    assert source.signal_12h_momentum(bearish_close) is False


def test_signal_6h_momentum_uses_latest_close_against_six_bars_ago() -> None:
    source = AutoResearchSource()

    bullish_close = pd.Series(np.linspace(100.0, 115.0, 16), dtype=float)
    bearish_close = pd.Series(np.linspace(115.0, 100.0, 16), dtype=float)

    assert source.signal_6h_momentum(bullish_close) is True
    assert source.signal_6h_momentum(bearish_close) is False


def test_signal_ema_crossover_uses_ema_7_over_ema_26() -> None:
    source = AutoResearchSource()

    bullish_close = pd.Series(
        np.concatenate([np.full(40, 100.0), np.linspace(101.0, 120.0, 20)]),
        dtype=float,
    )
    bearish_close = pd.Series(
        np.concatenate([np.full(40, 120.0), np.linspace(119.0, 100.0, 20)]),
        dtype=float,
    )

    assert source.signal_ema_crossover(bullish_close) is True
    assert source.signal_ema_crossover(bearish_close) is False


def test_signal_rsi_above_50_uses_eight_period_strength() -> None:
    source = AutoResearchSource()

    bullish_close = pd.Series(
        [100.0] * 12 + [101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0],
        dtype=float,
    )
    bearish_close = pd.Series(
        [108.0] * 12 + [107.0, 106.0, 105.0, 104.0, 103.0, 102.0, 101.0, 100.0],
        dtype=float,
    )

    assert source.signal_rsi_above_50(bullish_close) is True
    assert source.signal_rsi_above_50(bearish_close) is False


def test_signal_macd_histogram_positive_uses_14_23_9_macd() -> None:
    source = AutoResearchSource()

    bullish_close = pd.Series(
        np.concatenate([np.full(40, 100.0), np.linspace(101.0, 125.0, 20)]),
        dtype=float,
    )
    bearish_close = pd.Series(
        np.concatenate([np.full(40, 125.0), np.linspace(124.0, 100.0, 20)]),
        dtype=float,
    )

    assert source.signal_macd_histogram_positive(bullish_close) is True
    assert source.signal_macd_histogram_positive(bearish_close) is False


def test_signal_bollinger_width_identifies_compression_below_eighty_fifth_percentile() -> None:
    source = AutoResearchSource()

    compressed_close = pd.Series(
        np.concatenate(
            [
                100.0 + np.tile([12.0, -12.0], 30),
                np.linspace(100.0, 101.0, 20),
            ]
        ),
        dtype=float,
    )
    expanded_close = pd.Series(
        np.concatenate(
            [
                np.linspace(100.0, 101.0, 60),
                100.0 + np.tile([12.0, -12.0], 10),
            ]
        ),
        dtype=float,
    )

    assert source.signal_bollinger_width_compressed(compressed_close) is True
    assert source.signal_bollinger_width_compressed(expanded_close) is False


def test_build_snapshot_returns_bullish_for_four_of_six_votes() -> None:
    source = AutoResearchSource()
    signals = AutoResearchSignals(
        momentum_12h=True,
        momentum_6h=True,
        ema_crossover=True,
        rsi_above_50=True,
        macd_histogram_positive=False,
        bollinger_width_compressed=False,
    )

    snapshot = source.build_snapshot(pair="DOGE/USD", signals=signals)

    assert snapshot.direction is BeliefDirection.BULLISH
    assert snapshot.confidence == pytest.approx(0.67)
    assert snapshot.regime is MarketRegime.TRENDING
    assert snapshot.sources == (BeliefSource.AUTORESEARCH,)


def test_build_snapshot_returns_bearish_for_five_of_six_votes() -> None:
    source = AutoResearchSource()
    signals = AutoResearchSignals(
        momentum_12h=False,
        momentum_6h=False,
        ema_crossover=False,
        rsi_above_50=False,
        macd_histogram_positive=False,
        bollinger_width_compressed=True,
    )

    snapshot = source.build_snapshot(pair="DOGE/USD", signals=signals)

    assert snapshot.direction is BeliefDirection.BEARISH
    assert snapshot.confidence == pytest.approx(0.83)
    assert snapshot.regime is MarketRegime.RANGING
    assert snapshot.sources == (BeliefSource.AUTORESEARCH,)


def test_build_snapshot_returns_neutral_without_four_vote_majority() -> None:
    source = AutoResearchSource()
    signals = AutoResearchSignals(
        momentum_12h=True,
        momentum_6h=True,
        ema_crossover=True,
        rsi_above_50=False,
        macd_histogram_positive=False,
        bollinger_width_compressed=False,
    )

    snapshot = source.build_snapshot(pair="DOGE/USD", signals=signals)

    assert snapshot.direction is BeliefDirection.NEUTRAL
    assert snapshot.confidence == pytest.approx(0.5)
    assert snapshot.sources == (BeliefSource.AUTORESEARCH,)


def test_analyze_returns_belief_snapshot_from_ohlcv_bars() -> None:
    source = AutoResearchSource()
    close = np.concatenate(
        [
            np.linspace(100.0, 140.0, 60) + np.tile([4.0, -4.0], 30),
            np.linspace(141.0, 152.0, 20),
        ]
    )

    snapshot = source.analyze(pair="DOGE/USD", bars=make_bars(close))

    assert isinstance(snapshot, BeliefSnapshot)
    assert snapshot.pair == "DOGE/USD"
    assert snapshot.direction is BeliefDirection.BULLISH
    assert snapshot.sources == (BeliefSource.AUTORESEARCH,)
