"""Tests for research.cli -- argument parsing and main orchestration."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

from research.cli import parse_args, main


class TestParseArgs:
    def test_defaults(self):
        args = parse_args([])
        assert args.pair == "DOGE/USD"
        assert args.interval == 60
        assert args.since is None
        assert args.output_dir == "data/research"

    def test_custom_args(self):
        args = parse_args([
            "--pair", "BTC/USD",
            "--interval", "240",
            "--since", "1700000000",
            "--output-dir", "/tmp/out",
        ])
        assert args.pair == "BTC/USD"
        assert args.interval == 240
        assert args.since == 1700000000
        assert args.output_dir == "/tmp/out"


class TestMain:
    @patch("research.cli.DatasetBuilder")
    def test_main_calls_builder(self, MockBuilder):
        mock_instance = MagicMock()
        mock_instance.build_dataset.return_value = {
            "pair": "DOGE/USD",
            "row_count": 100,
            "timestamp_range": {"start": 1700000000, "end": 1700100000},
        }
        MockBuilder.return_value = mock_instance

        result = main(["--pair", "DOGE/USD", "--since", "1700000000"])

        assert result == 0
        mock_instance.build_dataset.assert_called_once()
        call_kwargs = mock_instance.build_dataset.call_args
        assert call_kwargs.kwargs["pair"] == "DOGE/USD"
        assert call_kwargs.kwargs["since"] == 1700000000

    @patch("research.cli.DatasetBuilder")
    def test_main_handles_error(self, MockBuilder):
        from research.dataset_builder import DatasetBuildError
        mock_instance = MagicMock()
        mock_instance.build_dataset.side_effect = DatasetBuildError("No data")
        MockBuilder.return_value = mock_instance

        result = main(["--pair", "DOGE/USD"])
        assert result == 1
