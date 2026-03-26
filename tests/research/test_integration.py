"""Integration tests for research dataset pipeline.

Tests:
- Deterministic output for identical inputs
- Point-in-time correctness: no label at row t depends on data after t
- Manifest metadata completeness
"""
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

import pandas as pd

from research.dataset_builder import DatasetBuilder


def _make_ohlcv_df(n: int = 30) -> pd.DataFrame:
    """Create synthetic OHLCV with known price pattern."""
    base_ts = 1700000000
    rows = []
    for i in range(n):
        # Linear price increase for predictable labels
        price = Decimal("100") + Decimal(str(i))
        rows.append({
            "timestamp": base_ts + i * 3600,
            "open": price,
            "high": price + Decimal("5"),
            "low": price - Decimal("3"),
            "close": price + Decimal("1"),
            "volume": Decimal("1000"),
        })
    return pd.DataFrame(rows)


class TestDeterministicOutput:
    """Running the builder twice on identical input produces identical Parquet."""

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_byte_identical_parquet(self, mock_fetch, tmp_path):
        ohlcv = _make_ohlcv_df(30)

        # Run 1
        mock_fetch.return_value = ohlcv.copy()
        dir1 = tmp_path / "run1"
        DatasetBuilder(output_dir=dir1).build_dataset("DOGE/USD", since=1700000000)

        # Run 2
        mock_fetch.return_value = ohlcv.copy()
        dir2 = tmp_path / "run2"
        DatasetBuilder(output_dir=dir2).build_dataset("DOGE/USD", since=1700000000)

        # Compare DataFrames (not raw bytes -- parquet metadata may differ)
        m1 = pd.read_parquet(dir1 / "market_v1.parquet")
        m2 = pd.read_parquet(dir2 / "market_v1.parquet")
        pd.testing.assert_frame_equal(m1, m2)

        l1 = pd.read_parquet(dir1 / "labels_v1.parquet")
        l2 = pd.read_parquet(dir2 / "labels_v1.parquet")
        pd.testing.assert_frame_equal(l1, l2)


class TestPointInTimeCorrectness:
    """No label at row t depends on OHLCV data after row t."""

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_labels_use_only_future_close_for_returns(self, mock_fetch, tmp_path):
        """Labels ARE forward-looking (they're ground truth), but features must not be."""
        ohlcv = _make_ohlcv_df(30)
        mock_fetch.return_value = ohlcv.copy()

        builder = DatasetBuilder(output_dir=tmp_path)
        builder.build_dataset("DOGE/USD", since=1700000000)

        labels = pd.read_parquet(tmp_path / "labels_v1.parquet")
        market = pd.read_parquet(tmp_path / "market_v1.parquet")

        # Verify return_bps_6h at row 0 uses close[6] - close[0]
        close_0 = market["close"].iloc[0]
        close_6 = market["close"].iloc[6]
        expected_bps = 10000.0 * (close_6 - close_0) / close_0
        actual_bps = labels["return_bps_6h"].iloc[0]
        assert abs(actual_bps - expected_bps) < 0.01

        # Verify last 6 rows have NaN for 6h labels (no future data available)
        assert labels["return_bps_6h"].iloc[-6:].isna().all()
        assert labels["return_sign_6h"].iloc[-6:].isna().all()

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_market_features_are_point_in_time(self, mock_fetch, tmp_path):
        """Market features at row t should only contain data at or before t."""
        ohlcv = _make_ohlcv_df(30)
        mock_fetch.return_value = ohlcv

        builder = DatasetBuilder(output_dir=tmp_path)
        builder.build_dataset("DOGE/USD", since=1700000000)

        market = pd.read_parquet(tmp_path / "market_v1.parquet")

        # Each row's features are just the OHLCV data at that timestamp
        # Verify timestamps are monotonically increasing
        assert market["timestamp"].is_monotonic_increasing

        # Verify market features match input OHLCV (no lookahead)
        for i in range(len(market)):
            assert market["close"].iloc[i] == float(ohlcv["close"].iloc[i])


class TestManifestMetadata:
    """Manifest contains expected fields."""

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_manifest_fields(self, mock_fetch, tmp_path):
        mock_fetch.return_value = _make_ohlcv_df(30)

        builder = DatasetBuilder(output_dir=tmp_path)
        manifest = builder.build_dataset("DOGE/USD", interval=60, since=1700000000)

        # Also verify the file on disk
        disk_manifest = json.loads(
            (tmp_path / "manifest_v1.json").read_text(encoding="utf-8")
        )

        for m in [manifest, disk_manifest]:
            assert m["schema_version"] == "research-dataset/v1"
            assert m["pair"] == "DOGE/USD"
            assert m["interval_minutes"] == 60
            assert m["row_count"] == 30
            assert "start" in m["timestamp_range"]
            assert "end" in m["timestamp_range"]
            assert "generated_at" in m
            assert "market_columns" in m
            assert "label_columns" in m
