from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pandas as pd
import pytest

from research.dataset_builder import DatasetBuilder, DatasetBuildError


def _make_ohlcv_df(n: int = 30) -> pd.DataFrame:
    """Create synthetic OHLCV DataFrame."""
    base_ts = 1700000000
    rows = []
    for i in range(n):
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


class TestBuildDataset:
    """Dataset builder writes correct Parquet and manifest files."""

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_creates_output_files(self, mock_fetch, tmp_path):
        mock_fetch.return_value = _make_ohlcv_df(30)

        builder = DatasetBuilder(output_dir=tmp_path)
        builder.build_dataset("DOGE/USD", interval=60, since=1700000000)

        assert (tmp_path / "market_v1.parquet").exists()
        assert (tmp_path / "labels_v1.parquet").exists()
        assert (tmp_path / "manifest_v1.json").exists()

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_manifest_content(self, mock_fetch, tmp_path):
        mock_fetch.return_value = _make_ohlcv_df(30)

        builder = DatasetBuilder(output_dir=tmp_path)
        manifest = builder.build_dataset("DOGE/USD", interval=60, since=1700000000)

        assert manifest["pair"] == "DOGE/USD"
        assert manifest["interval_minutes"] == 60
        assert manifest["row_count"] == 30
        assert "schema_version" in manifest
        assert "generated_at" in manifest
        assert "timestamp_range" in manifest

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_parquet_structure(self, mock_fetch, tmp_path):
        mock_fetch.return_value = _make_ohlcv_df(30)

        builder = DatasetBuilder(output_dir=tmp_path)
        builder.build_dataset("DOGE/USD", interval=60, since=1700000000)

        market = pd.read_parquet(tmp_path / "market_v1.parquet")
        labels = pd.read_parquet(tmp_path / "labels_v1.parquet")

        assert len(market) == 30
        assert "timestamp" in market.columns
        assert "open" in market.columns
        assert "close" in market.columns

        assert len(labels) == 30
        assert "return_bps_6h" in labels.columns
        assert "return_sign_6h" in labels.columns
        assert "regime_label" in labels.columns

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_deterministic_output(self, mock_fetch, tmp_path):
        """Same input produces identical Parquet files."""
        ohlcv = _make_ohlcv_df(30)
        mock_fetch.return_value = ohlcv

        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        builder1 = DatasetBuilder(output_dir=dir1)
        builder1.build_dataset("DOGE/USD", interval=60, since=1700000000)

        mock_fetch.return_value = ohlcv.copy()
        builder2 = DatasetBuilder(output_dir=dir2)
        builder2.build_dataset("DOGE/USD", interval=60, since=1700000000)

        m1 = pd.read_parquet(dir1 / "market_v1.parquet")
        m2 = pd.read_parquet(dir2 / "market_v1.parquet")
        pd.testing.assert_frame_equal(m1, m2)

        l1 = pd.read_parquet(dir1 / "labels_v1.parquet")
        l2 = pd.read_parquet(dir2 / "labels_v1.parquet")
        pd.testing.assert_frame_equal(l1, l2)

    @patch("research.dataset_builder.fetch_ohlcv_history")
    def test_empty_ohlcv_raises(self, mock_fetch, tmp_path):
        mock_fetch.return_value = pd.DataFrame(
            columns=["timestamp", "open", "high", "low", "close", "volume"]
        )

        builder = DatasetBuilder(output_dir=tmp_path)
        with pytest.raises(DatasetBuildError, match="No OHLCV data"):
            builder.build_dataset("DOGE/USD", interval=60, since=1700000000)
