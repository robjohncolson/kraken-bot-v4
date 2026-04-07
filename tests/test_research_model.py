"""Tests for the generic research model source and handler."""

import json
from pathlib import Path
from unittest.mock import patch

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from beliefs.research_model_handler import make_research_model_handler, make_shadow_handler
from beliefs.research_model_source import (
    ArtifactLoadError,
    ResearchModelSource,
)
from core.types import BeliefDirection, BeliefSource, MarketRegime


def _make_ohlcv_bars(n: int = 50) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({
        "open": close - rng.uniform(0.1, 0.5, n),
        "high": close + rng.uniform(0.1, 0.5, n),
        "low": close - rng.uniform(0.5, 1.0, n),
        "close": close,
        "volume": rng.uniform(1000, 5000, n),
    })


def _make_artifact(tmp: Path) -> Path:
    """Create a minimal valid artifact directory with fitted model."""
    artifact_dir = tmp / "test_artifact"
    model_dir = artifact_dir / "model"
    model_dir.mkdir(parents=True)

    # Manifest
    manifest = {
        "artifact_id": "test_logistic_20260329_abc123",
        "artifact_version": "1.0",
        "model_family": "logistic_regression",
        "input_schema_version": "market/v1",
        "output_schema_version": "prediction/v1",
        "label_horizon": "6h",
        "calibration": {"method": "none"},
        "evaluation_summary": {"net_pnl_bps": 5531.22},
        "source_commit": "abc123",
        "experiment_id": "abc123",
        "created_at": "2026-03-29T00:00:00+00:00",
        "data_source": {"source": "cryptocompare", "exchange": "Kraken"},
    }
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest))

    # Fit a real model on synthetic data
    bars = _make_ohlcv_bars(200)
    close = bars["close"].astype(float)
    open_ = bars["open"].astype(float)
    high = bars["high"].astype(float)
    low = bars["low"].astype(float)
    volume = bars["volume"].astype(float)

    features = pd.DataFrame({
        "ret_1": close.pct_change(1),
        "ret_6": close.pct_change(6),
        "ret_12": close.pct_change(12),
        "hl_range": (high - low) / close,
        "co_range": (close - open_) / open_,
        "vol_ratio": volume / volume.rolling(20, min_periods=1).mean(),
        "volatility": close.pct_change(1).rolling(12, min_periods=1).std(),
    }).fillna(0.0)

    X = features.values
    y = np.random.default_rng(42).integers(0, 2, len(X))

    scaler = StandardScaler().fit(X)
    model = LogisticRegression(max_iter=1000, random_state=42, C=1.0)
    model.fit(scaler.transform(X), y)

    joblib.dump(scaler, model_dir / "scaler.pkl")
    joblib.dump(model, model_dir / "model.pkl")

    meta = {"feature_names": list(features.columns), "feature_count": 7, "threshold": 0.55}
    (model_dir / "meta.json").write_text(json.dumps(meta))

    return artifact_dir


class TestResearchModelSource:
    def test_load_valid_artifact(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        source = ResearchModelSource(artifact_dir)
        assert source.artifact_id == "test_logistic_20260329_abc123"
        assert source.min_bars == 20

    def test_analyze_returns_belief_snapshot(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        source = ResearchModelSource(artifact_dir)
        bars = _make_ohlcv_bars(50)
        result = source.analyze("DOGE/USD", bars)
        assert result is not None
        assert result.pair == "DOGE/USD"
        assert result.direction in (BeliefDirection.BULLISH, BeliefDirection.BEARISH, BeliefDirection.NEUTRAL)
        assert 0.0 <= result.confidence <= 1.0
        assert result.regime == MarketRegime.UNKNOWN
        assert BeliefSource.RESEARCH_MODEL in result.sources

    def test_analyze_insufficient_bars(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        source = ResearchModelSource(artifact_dir)
        bars = _make_ohlcv_bars(10)
        result = source.analyze("DOGE/USD", bars)
        assert result is None

    def test_predict_raw_returns_float(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        source = ResearchModelSource(artifact_dir)
        bars = _make_ohlcv_bars(50)
        prob = source.predict_raw(bars)
        assert prob is not None
        assert 0.0 <= prob <= 1.0

    def test_missing_manifest_raises(self, tmp_path: Path) -> None:
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(ArtifactLoadError, match="manifest.json not found"):
            ResearchModelSource(empty_dir)

    def test_bad_schema_raises(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        manifest = json.loads((artifact_dir / "manifest.json").read_text())
        manifest["input_schema_version"] = "bad/v99"
        (artifact_dir / "manifest.json").write_text(json.dumps(manifest))
        with pytest.raises(ArtifactLoadError, match="Unsupported input schema"):
            ResearchModelSource(artifact_dir)

    def test_missing_model_pkl_raises(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        (artifact_dir / "model" / "model.pkl").unlink()
        with pytest.raises(ArtifactLoadError, match="model.pkl not found"):
            ResearchModelSource(artifact_dir)


class TestResearchModelHandler:
    def test_handler_returns_belief(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        handler = make_research_model_handler(artifact_dir)

        bars = _make_ohlcv_bars(50)
        with patch("beliefs.research_model_handler.fetch_ohlcv", return_value=bars):
            from scheduler import BeliefRefreshRequest
            from datetime import datetime, timezone

            request = BeliefRefreshRequest(
                pair="DOGE/USD",
                position_id="",
                checked_at=datetime.now(timezone.utc),
                stale_after_hours=4,
            )
            result = handler(request)
            assert result is not None
            assert result.pair == "DOGE/USD"

    def test_shadow_handler_logs_and_returns_none(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        handler = make_shadow_handler(artifact_dir)

        bars = _make_ohlcv_bars(50)
        with patch("beliefs.research_model_handler.fetch_ohlcv", return_value=bars):
            from scheduler import BeliefRefreshRequest
            from datetime import datetime, timezone

            request = BeliefRefreshRequest(
                pair="DOGE/USD",
                position_id="",
                checked_at=datetime.now(timezone.utc),
                stale_after_hours=4,
            )
            # Shadow handler returns None (logs only)
            result = handler(request)
            assert result is None

    def test_handler_returns_none_on_fetch_error(self, tmp_path: Path) -> None:
        artifact_dir = _make_artifact(tmp_path)
        handler = make_research_model_handler(artifact_dir)

        from exchange.ohlcv import OHLCVFetchError
        with patch("beliefs.research_model_handler.fetch_ohlcv", side_effect=OHLCVFetchError("test")):
            from scheduler import BeliefRefreshRequest
            from datetime import datetime, timezone

            request = BeliefRefreshRequest(
                pair="DOGE/USD",
                position_id="",
                checked_at=datetime.now(timezone.utc),
                stale_after_hours=4,
            )
            result = handler(request)
            assert result is None
