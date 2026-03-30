"""Generic artifact-driven belief source.

Loads any promoted research artifact via ``ACTIVE_ARTIFACT_ID``,
validates the manifest, loads serialized model weights, and maps
OHLCV bars to ``BeliefSnapshot`` objects.

This module is model-family agnostic: it loads whatever scaler + model
are in the artifact's ``model/`` directory.  The feature pipeline is
currently V1 (7 OHLCV features); future artifact versions can override
via ``meta.json``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    MarketRegime,
)

logger = logging.getLogger(__name__)

REQUIRED_OHLCV_COLUMNS = {"open", "high", "low", "close", "volume"}
MIN_BARS = 20  # V1 vol_ratio needs rolling(20)


class ResearchModelError(Exception):
    """Base exception for research model source errors."""


class ArtifactLoadError(ResearchModelError):
    """Raised when the artifact cannot be loaded or validated."""


def _build_v1_features(market: pd.DataFrame) -> np.ndarray:
    """Build V1 feature array from OHLCV bars.

    Same pipeline as autoresearch's ``_build_features``.
    """
    close = market["close"].astype(float)
    open_ = market["open"].astype(float)
    high = market["high"].astype(float)
    low = market["low"].astype(float)
    volume = market["volume"].astype(float)

    features = pd.DataFrame({
        "ret_1": close.pct_change(1),
        "ret_6": close.pct_change(6),
        "ret_12": close.pct_change(12),
        "hl_range": (high - low) / close,
        "co_range": (close - open_) / open_,
        "vol_ratio": volume / volume.rolling(20, min_periods=1).mean(),
        "volatility": close.pct_change(1).rolling(12, min_periods=1).std(),
    })
    return features.fillna(0.0).values


class ResearchModelSource:
    """Generic artifact consumer implementing the BeliefAnalyzer protocol.

    Loads a promoted artifact directory containing:
    - ``manifest.json`` with input/output schema validation
    - ``model/scaler.pkl`` (StandardScaler)
    - ``model/model.pkl`` (fitted sklearn estimator)
    - ``model/meta.json`` (feature names, threshold)
    """

    def __init__(self, artifact_dir: Path) -> None:
        self._artifact_dir = Path(artifact_dir)
        self._manifest = self._load_manifest()
        self._meta = self._load_meta()
        self._scaler = self._load_pkl("scaler.pkl")
        self._model = self._load_pkl("model.pkl")
        self._threshold: float = self._meta.get("threshold", 0.55)
        self.threshold = self._threshold
        logger.info(
            "Research model loaded: artifact=%s family=%s features=%d threshold=%.2f",
            self._manifest.get("artifact_id", "unknown"),
            self._manifest.get("model_family", "unknown"),
            self._meta.get("feature_count", -1),
            self._threshold,
        )

    @property
    def min_bars(self) -> int:
        return MIN_BARS

    @property
    def artifact_id(self) -> str:
        return self._manifest.get("artifact_id", "unknown")

    @property
    def manifest(self) -> dict:
        return dict(self._manifest)

    def analyze(
        self, pair: str, bars: pd.DataFrame, **kwargs: object,
    ) -> BeliefSnapshot | None:
        """Run inference on OHLCV bars and return a BeliefSnapshot.

        Returns ``None`` on any error (never crashes the runtime).
        Also returns the raw ``prob_up`` via the ``last_prob_up``
        attribute for shadow-mode telemetry.
        """
        try:
            return self._predict(pair, bars)
        except Exception:
            logger.warning(
                "Research model prediction failed for %s (artifact=%s)",
                pair, self.artifact_id, exc_info=True,
            )
            return None

    def predict_raw(self, bars: pd.DataFrame) -> float | None:
        """Return raw prob_up without mapping to BeliefSnapshot.

        Used by shadow handler to log prob_up separately.
        """
        try:
            missing = REQUIRED_OHLCV_COLUMNS - set(bars.columns)
            if missing:
                return None
            if len(bars) < self.min_bars:
                return None
            X = _build_v1_features(bars)
            X_last = X[-1:].reshape(1, -1)
            X_scaled = self._scaler.transform(X_last)
            prob_up = float(self._model.predict_proba(X_scaled)[0, 1])
            return prob_up
        except Exception:
            return None

    def _predict(self, pair: str, bars: pd.DataFrame) -> BeliefSnapshot | None:
        missing = REQUIRED_OHLCV_COLUMNS - set(bars.columns)
        if missing:
            logger.warning("Missing OHLCV columns: %s", missing)
            return None

        if len(bars) < self.min_bars:
            logger.warning(
                "Insufficient bars: %d < %d", len(bars), self.min_bars,
            )
            return None

        X = _build_v1_features(bars)
        X_last = X[-1:].reshape(1, -1)
        X_scaled = self._scaler.transform(X_last)
        prob_up = float(self._model.predict_proba(X_scaled)[0, 1])

        # Store for telemetry access
        self._last_prob_up = prob_up

        # Map to signal using threshold
        if prob_up > self._threshold:
            direction = BeliefDirection.BULLISH
        elif prob_up < (1.0 - self._threshold):
            direction = BeliefDirection.BEARISH
        else:
            direction = BeliefDirection.NEUTRAL

        confidence = round(abs(prob_up - 0.5) * 2, 4)

        return BeliefSnapshot(
            pair=pair,
            direction=direction,
            confidence=confidence,
            regime=MarketRegime.UNKNOWN,
            sources=(BeliefSource.RESEARCH_MODEL,),
        )

    def _load_manifest(self) -> dict:
        manifest_path = self._artifact_dir / "manifest.json"
        if not manifest_path.exists():
            raise ArtifactLoadError(
                f"manifest.json not found in {self._artifact_dir}"
            )
        with open(manifest_path) as f:
            manifest = json.load(f)

        # Validate schemas
        if manifest.get("input_schema_version") != "market/v1":
            raise ArtifactLoadError(
                f"Unsupported input schema: {manifest.get('input_schema_version')}"
            )
        if manifest.get("output_schema_version") != "prediction/v1":
            raise ArtifactLoadError(
                f"Unsupported output schema: {manifest.get('output_schema_version')}"
            )
        return manifest

    def _load_meta(self) -> dict:
        meta_path = self._artifact_dir / "model" / "meta.json"
        if not meta_path.exists():
            return {}
        with open(meta_path) as f:
            return json.load(f)

    def _load_pkl(self, filename: str) -> object:
        pkl_path = self._artifact_dir / "model" / filename
        if not pkl_path.exists():
            raise ArtifactLoadError(f"{filename} not found in {self._artifact_dir / 'model'}")
        return joblib.load(pkl_path)


__all__ = [
    "ArtifactLoadError",
    "ResearchModelError",
    "ResearchModelSource",
]
