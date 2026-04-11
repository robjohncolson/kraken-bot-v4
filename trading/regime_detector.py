"""HMM-based market regime detector.

Fits a 3-state Gaussian HMM to OHLCV bars and returns regime probabilities.
States are labeled post-hoc by their statistics:
  - Trending: high trend efficiency, moderate volatility
  - Ranging: low trend efficiency, low volatility (fees kill you here)
  - Volatile: high volatility, low efficiency (chop/dislocation)

Design informed by CC + Codex council (2026-04-11). Direction (up/down)
is handled separately by EMA; the HMM answers "should I trade at all?"

Usage:
    from trading.regime_detector import detect_regime
    result = detect_regime(bars_df)
    # result.regime = "trending", result.confidence = 0.82
    # result.probabilities = {"trending": 0.82, "ranging": 0.05, "volatile": 0.13}
    # result.trade_gate = 0.87  (1 - P(ranging), use as confidence multiplier)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from hmmlearn.hmm import GaussianHMM

logger = logging.getLogger(__name__)

N_STATES = 3
MIN_BARS = 50
EFFICIENCY_WINDOW = 8  # bars for trend efficiency calculation


@dataclass(frozen=True)
class RegimeResult:
    regime: str  # "trending", "ranging", "volatile"
    confidence: float  # P(current state)
    probabilities: dict[str, float]
    trade_gate: float  # 1 - P(ranging): multiply with signal confidence


def _extract_features(bars: pd.DataFrame) -> np.ndarray:
    """Extract 3 features: log returns, Parkinson volatility, trend efficiency."""
    close = bars["close"].astype(float).values
    high = bars["high"].astype(float).values
    low = bars["low"].astype(float).values

    n = len(close)

    # Feature 1: log returns
    log_returns = np.diff(np.log(close))

    # Feature 2: Parkinson volatility (high-low range, more efficient than close-close)
    hl_ratio = np.log(high[1:] / low[1:])
    parkinson_vol = hl_ratio / (2 * np.sqrt(np.log(2)))

    # Feature 3: Trend efficiency ratio over EFFICIENCY_WINDOW bars
    # = abs(net move) / sum(abs(individual moves))
    # 1.0 = perfectly clean trend, 0.0 = pure chop
    efficiency = np.zeros(n - 1)
    abs_diffs = np.abs(np.diff(close))
    for i in range(n - 1):
        start = max(0, i - EFFICIENCY_WINDOW + 1)
        window_sum = abs_diffs[start:i + 1].sum()
        net_move = abs(close[i + 1] - close[max(0, start)])
        efficiency[i] = net_move / window_sum if window_sum > 0 else 0

    features = np.column_stack([log_returns, parkinson_vol, efficiency])
    return features


def _label_states(model: GaussianHMM) -> dict[int, str]:
    """Label states by their characteristics.

    Trending = highest trend efficiency
    Volatile = highest volatility (Parkinson)
    Ranging = the remaining state
    """
    efficiencies = model.means_[:, 2]  # Mean trend efficiency
    volatilities = model.means_[:, 1]  # Mean Parkinson vol

    labels: dict[int, str] = {}
    trending_idx = int(np.argmax(efficiencies))
    labels[trending_idx] = "trending"

    remaining = [i for i in range(model.n_components) if i != trending_idx]
    if len(remaining) == 2:
        # Of the remaining, higher vol = volatile, lower = ranging
        if volatilities[remaining[0]] > volatilities[remaining[1]]:
            labels[remaining[0]] = "volatile"
            labels[remaining[1]] = "ranging"
        else:
            labels[remaining[0]] = "ranging"
            labels[remaining[1]] = "volatile"
    elif remaining:
        labels[remaining[0]] = "ranging"

    return labels


def detect_regime(
    bars: pd.DataFrame,
    *,
    n_states: int = N_STATES,
    n_iter: int = 50,
) -> RegimeResult:
    """Fit HMM to OHLCV bars and return the current regime.

    Args:
        bars: DataFrame with open, high, low, close columns. Minimum 50 rows.
        n_states: Number of hidden states (default 3).
        n_iter: EM iterations for fitting.

    Returns:
        RegimeResult with regime, confidence, probabilities, and trade_gate.
    """
    unknown = RegimeResult(
        regime="unknown", confidence=0.0,
        probabilities={"trending": 0.33, "ranging": 0.34, "volatile": 0.33},
        trade_gate=0.66,
    )
    if len(bars) < MIN_BARS:
        return unknown

    features = _extract_features(bars)

    # Standardize features for numerical stability
    feat_mean = features.mean(axis=0)
    feat_std = features.std(axis=0)
    feat_std[feat_std < 1e-10] = 1.0  # Avoid division by zero
    features_scaled = (features - feat_mean) / feat_std

    model = GaussianHMM(
        n_components=n_states,
        covariance_type="diag",
        n_iter=n_iter,
        random_state=42,
    )
    try:
        model.fit(features_scaled)
    except Exception as exc:
        logger.warning("HMM fit failed: %s", exc)
        return unknown

    posteriors = model.predict_proba(features_scaled)
    current_probs = posteriors[-1]

    labels = _label_states(model)
    labeled_probs = {labels[i]: float(current_probs[i]) for i in range(n_states)}

    regime = max(labeled_probs, key=labeled_probs.get)
    confidence = labeled_probs[regime]

    # Trade gate: soft filter — 1 minus P(ranging)
    # When ranging, this drops toward 0, suppressing entry confidence
    trade_gate = 1.0 - labeled_probs.get("ranging", 0.0)

    return RegimeResult(
        regime=regime,
        confidence=round(confidence, 4),
        probabilities={k: round(v, 4) for k, v in labeled_probs.items()},
        trade_gate=round(trade_gate, 4),
    )
