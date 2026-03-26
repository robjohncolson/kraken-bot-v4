"""Forward-looking label computation for OHLCV data."""
from __future__ import annotations

import numpy as np
import pandas as pd


class LabelComputationError(Exception):
    """Raised when label computation fails."""


def compute_labels(
    ohlcv_df: pd.DataFrame,
    horizons: list[int] | None = None,
    vol_lookback: int = 24,
) -> pd.DataFrame:
    """Compute forward-looking labels from OHLCV data.

    Args:
        ohlcv_df: DataFrame with 'close' column and 'timestamp' column.
                  Assumed sorted by timestamp ascending.
        horizons: List of horizon periods in rows
                  (default [6, 12] for 6h/12h with hourly candles).
        vol_lookback: Rolling window size for realized volatility
                      (default 24 for 24h with hourly candles).

    Returns:
        DataFrame with columns:
        - return_sign_{h}h: 1 for positive, -1 for negative, 0 for zero
        - return_bps_{h}h: return in basis points
        - regime_label: 'low', 'medium', or 'high' based on realized volatility

    Raises:
        LabelComputationError: If input validation fails.
    """
    if horizons is None:
        horizons = [6, 12]

    _validate_input(ohlcv_df, horizons, vol_lookback)

    result = pd.DataFrame(index=ohlcv_df.index)
    close = ohlcv_df["close"].astype(float)

    # Forward return labels for each horizon
    for h in horizons:
        future_close = close.shift(-h)
        return_bps = 10000.0 * (future_close - close) / close
        result[f"return_bps_{h}h"] = return_bps
        result[f"return_sign_{h}h"] = np.sign(return_bps)

    # Regime label from rolling realized volatility
    result["regime_label"] = _compute_regime_labels(close, vol_lookback)

    return result


def _validate_input(
    ohlcv_df: pd.DataFrame,
    horizons: list[int],
    vol_lookback: int,
) -> None:
    """Validate inputs to compute_labels.

    Raises:
        LabelComputationError: On invalid input.
    """
    if not isinstance(ohlcv_df, pd.DataFrame):
        raise LabelComputationError("ohlcv_df must be a pandas DataFrame")

    if "close" not in ohlcv_df.columns:
        raise LabelComputationError("ohlcv_df must contain a 'close' column")

    if ohlcv_df.empty:
        raise LabelComputationError("ohlcv_df must not be empty")

    if not horizons:
        raise LabelComputationError("horizons must be a non-empty list of positive integers")

    for h in horizons:
        if not isinstance(h, int) or h <= 0:
            raise LabelComputationError(
                f"Each horizon must be a positive integer, got {h!r}"
            )

    if not isinstance(vol_lookback, int) or vol_lookback <= 1:
        raise LabelComputationError(
            f"vol_lookback must be an integer > 1, got {vol_lookback!r}"
        )


def _compute_regime_labels(close: pd.Series, lookback: int) -> pd.Series:
    """Classify volatility regime into terciles.

    Uses rolling realized volatility of log returns over *lookback* rows.
    Bottom tercile = 'low', middle = 'medium', top = 'high'.
    Rows with insufficient data are NaN.
    """
    log_returns = np.log(close / close.shift(1))
    rolling_vol = log_returns.rolling(window=lookback).std()

    # pd.qcut on non-NaN values into 3 bins
    valid = rolling_vol.dropna()
    if valid.empty:
        return pd.Series(np.nan, index=close.index, dtype=object)

    try:
        terciles = pd.qcut(
            valid, q=3, labels=["low", "medium", "high"], duplicates="drop"
        )
    except ValueError as exc:
        raise LabelComputationError(
            f"Failed to compute regime terciles: {exc}"
        ) from exc

    regime = pd.Series(np.nan, index=close.index, dtype=object)
    regime.loc[terciles.index] = terciles.astype(str)
    return regime
