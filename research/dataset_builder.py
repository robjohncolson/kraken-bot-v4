from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research.ohlcv_history import fetch_ohlcv_history
from research.db_reader import ResearchReader
from research.labels import compute_labels

logger = logging.getLogger(__name__)


class DatasetBuildError(Exception):
    """Raised when dataset construction fails."""


class DatasetBuilder:
    """Assembles market features and labels into versioned Parquet files."""

    def __init__(
        self,
        output_dir: Path | str = "data/research",
        schema_version: str = "v1",
    ) -> None:
        self._output_dir = Path(output_dir)
        self._schema_version = schema_version

    def build_dataset(
        self,
        pair: str,
        interval: int = 60,
        since: int | None = None,
        until: int | None = None,
        db_reader: ResearchReader | None = None,
    ) -> dict[str, Any]:
        """Build and export the research dataset.

        Steps:
        1. Fetch OHLCV history via fetch_ohlcv_history
        2. Optionally merge trade data from DB reader
        3. Compute labels
        4. Split into market features (point-in-time only) and labels
        5. Write market_v1.parquet, labels_v1.parquet, manifest_v1.json

        Returns:
            manifest dict with metadata about the export
        """
        # Fetch OHLCV
        ohlcv_df = fetch_ohlcv_history(pair, interval, since=since or 0, until=until)
        if ohlcv_df.empty:
            raise DatasetBuildError(f"No OHLCV data for {pair}")

        # Market features: point-in-time OHLCV columns
        # These are available at time t (the candle is already closed)
        market_df = ohlcv_df.copy()

        # Add trade-derived features if DB reader provided
        if db_reader is not None:
            fills = db_reader.fetch_fills()
            if not fills.empty:
                # Add fill count per period as a feature
                # (this is point-in-time: fills that happened at or before time t)
                market_df = self._merge_fill_features(market_df, fills, interval)

        # Compute forward-looking labels
        labels_df = compute_labels(ohlcv_df)

        # Ensure same index alignment
        labels_df.index = market_df.index

        # Write outputs
        self._output_dir.mkdir(parents=True, exist_ok=True)

        market_path = self._output_dir / f"market_{self._schema_version}.parquet"
        labels_path = self._output_dir / f"labels_{self._schema_version}.parquet"
        manifest_path = self._output_dir / f"manifest_{self._schema_version}.json"

        # Convert Decimals to float for parquet compatibility
        float_market = market_df.copy()
        for col in ["open", "high", "low", "close", "volume"]:
            if col in float_market.columns:
                float_market[col] = float_market[col].astype(float)

        float_market.to_parquet(market_path, index=False)
        labels_df.to_parquet(labels_path, index=False)

        # Build manifest
        manifest = {
            "schema_version": f"research-dataset/{self._schema_version}",
            "pair": pair,
            "interval_minutes": interval,
            "row_count": len(market_df),
            "timestamp_range": {
                "start": int(market_df["timestamp"].iloc[0]),
                "end": int(market_df["timestamp"].iloc[-1]),
            },
            "market_columns": list(float_market.columns),
            "label_columns": list(labels_df.columns),
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        logger.info(
            "Dataset exported: %d rows, %s -> %s",
            len(market_df),
            market_path,
            labels_path,
        )

        return manifest

    @staticmethod
    def _merge_fill_features(
        market_df: pd.DataFrame,
        fills: pd.DataFrame,
        interval: int,
    ) -> pd.DataFrame:
        """Merge fill-derived features into market DataFrame (point-in-time only).

        For each market row at time t, count fills that happened at or before t.
        This is a cumulative count -- a valid point-in-time feature.
        """
        if fills.empty or "filled_at" not in fills.columns:
            return market_df

        result = market_df.copy()
        # Simple cumulative fill count feature
        # Convert filled_at to timestamps for comparison
        try:
            fill_times = pd.to_datetime(fills["filled_at"]).astype("int64") // 10**9
            result["cumulative_fill_count"] = 0
            for i, row in result.iterrows():
                t = row["timestamp"]
                result.at[i, "cumulative_fill_count"] = int((fill_times <= t).sum())
        except (ValueError, TypeError):
            # If fill timestamps can't be parsed, skip this feature
            pass
        return result


__all__ = [
    "DatasetBuildError",
    "DatasetBuilder",
]
