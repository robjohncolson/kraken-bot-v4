"""Tests for backfill shadow validation."""

from __future__ import annotations

import json
import pickle
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from research.backfill_shadow import BackfillResult, run_backfill


def _create_synthetic_artifact(tmp_path: Path) -> Path:
    """Create a minimal V1 LogReg artifact for testing."""
    artifact_dir = tmp_path / "test_artifact"
    model_dir = artifact_dir / "model"
    model_dir.mkdir(parents=True)

    # Manifest
    manifest = {
        "artifact_id": "test_logreg_backfill",
        "model_family": "logistic_regression",
        "input_schema_version": "market/v1",
        "output_schema_version": "prediction/v1",
        "label_horizon": "6h",
    }
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

    # Train a simple model
    rng = np.random.RandomState(42)
    X = rng.randn(200, 7)
    y = (X[:, 0] > 0).astype(int)

    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    model = LogisticRegression(max_iter=1000, random_state=42)
    model.fit(X_scaled, y)

    with open(model_dir / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)
    with open(model_dir / "model.pkl", "wb") as f:
        pickle.dump(model, f)

    meta = {"threshold": 0.55, "features": ["ret_1", "ret_6", "ret_12", "hl_range", "co_range", "vol_ratio", "volatility"]}
    (model_dir / "meta.json").write_text(json.dumps(meta))

    return artifact_dir


def _create_synthetic_dataset(tmp_path: Path, n_bars: int = 100) -> Path:
    """Create synthetic OHLCV + labels parquet files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    rng = np.random.RandomState(42)
    base_price = 0.18
    prices = base_price + np.cumsum(rng.randn(n_bars) * 0.001)
    prices = np.maximum(prices, 0.01)

    market = pd.DataFrame({
        "timestamp": np.arange(1700000000, 1700000000 + n_bars * 3600, 3600),
        "open": prices * (1 + rng.randn(n_bars) * 0.001),
        "high": prices * (1 + abs(rng.randn(n_bars) * 0.002)),
        "low": prices * (1 - abs(rng.randn(n_bars) * 0.002)),
        "close": prices,
        "volume": rng.uniform(100, 1000, n_bars),
    })

    # Forward-looking labels
    returns_6h = np.zeros(n_bars)
    signs_6h = np.zeros(n_bars, dtype=int)
    for i in range(n_bars - 6):
        ret = (prices[i + 6] - prices[i]) / prices[i] * 10000
        returns_6h[i] = ret
        signs_6h[i] = 1 if ret > 0 else (-1 if ret < 0 else 0)
    returns_6h[-6:] = np.nan
    signs_6h[-6:] = 0

    labels = pd.DataFrame({
        "return_bps_6h": returns_6h,
        "return_sign_6h": signs_6h,
    })

    market.to_parquet(data_dir / "market_v1.parquet", index=False)
    labels.to_parquet(data_dir / "labels_v1.parquet", index=False)

    return data_dir


def test_backfill_result_properties() -> None:
    result = BackfillResult(
        total_bars=100,
        predictions=95,
        abstains=10,
        matched=80,
        correct_direction=45,
        paper_pnl_bps=500.0,
        trades_positive=42,
        trades_total=80,
    )
    assert result.coverage == pytest.approx(0.95)
    assert result.abstain_rate == pytest.approx(10 / 95)
    assert result.directional_accuracy == pytest.approx(45 / 80)
    assert result.hit_rate == pytest.approx(42 / 80)
    assert result.mean_pnl_per_trade == pytest.approx(500 / 80)


def test_backfill_result_gates_pass() -> None:
    result = BackfillResult(
        total_bars=100, predictions=95, abstains=5,
        matched=85, correct_direction=50, paper_pnl_bps=1000.0,
        trades_positive=50, trades_total=85,
    )
    gates = result.passes_gates()
    assert gates["coverage_>90%"] is True
    assert gates["accuracy_>50%"] is True


def test_backfill_result_gates_fail() -> None:
    result = BackfillResult(
        total_bars=100, predictions=80, abstains=40,
        matched=35, correct_direction=15, paper_pnl_bps=-500.0,
        trades_positive=10, trades_total=35,
    )
    gates = result.passes_gates()
    assert gates["coverage_>90%"] is False
    assert gates["accuracy_>50%"] is False


def test_run_backfill_produces_results(tmp_path: Path) -> None:
    artifact_dir = _create_synthetic_artifact(tmp_path)
    data_dir = _create_synthetic_dataset(tmp_path, n_bars=100)

    result = run_backfill(artifact_dir, data_dir)

    assert result.total_bars == 51  # 100 - 50 + 1
    assert result.predictions > 0
    assert result.predictions <= result.total_bars
    assert result.abstains >= 0
    assert result.trades_total >= 0
    assert result.trades_total <= result.matched


def test_run_backfill_empty_result() -> None:
    result = BackfillResult(
        total_bars=0, predictions=0, abstains=0,
        matched=0, correct_direction=0, paper_pnl_bps=0.0,
        trades_positive=0, trades_total=0,
    )
    assert result.coverage == 0.0
    assert result.directional_accuracy == 0.0
    assert result.hit_rate == 0.0
    assert result.mean_pnl_per_trade == 0.0
