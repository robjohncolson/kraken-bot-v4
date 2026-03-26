"""Tests for research.ohlcv_history module."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from research.ohlcv_history import OHLCVHistoryError, fetch_ohlcv_history


def _make_candle(ts: int, price: str = "0.08", volume: str = "1000.0"):
    """Build a single Kraken OHLC candle array."""
    return [ts, price, "0.09", "0.07", "0.085", "0.084", volume, 50]


def _mock_response(candles: list, last: int, pair: str = "XDGUSD"):
    """Create a mock httpx.Response with Kraken OHLC JSON."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "error": [],
        "result": {
            pair: candles,
            "last": last,
        },
    }
    return resp


def _mock_error_response(errors: list[str]):
    """Create a mock httpx.Response with Kraken error."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "error": errors,
        "result": {},
    }
    return resp


class TestSinglePageFetch:
    """test_single_page_fetch: one response with 3 candles."""

    @patch("research.ohlcv_history.httpx.get")
    def test_single_page_fetch(self, mock_get):
        candles = [
            _make_candle(1700000000, "0.08", "1000.0"),
            _make_candle(1700003600, "0.09", "2000.0"),
            _make_candle(1700007200, "0.10", "3000.0"),
        ]
        # Second response returns empty candles to stop pagination
        mock_get.side_effect = [
            _mock_response(candles, last=1700007200),
            _mock_response([], last=1700007200),
        ]

        df = fetch_ohlcv_history("DOGE/USD", interval=60, since=1700000000)

        assert df.shape == (3, 6)
        assert list(df.columns) == [
            "timestamp", "open", "high", "low", "close", "volume",
        ]
        assert df["open"].iloc[0] == Decimal("0.08")
        assert df["volume"].iloc[2] == Decimal("3000.0")


class TestPagination:
    """test_pagination: two pages, first has 720 candles, second has 3."""

    @patch("research.ohlcv_history.httpx.get")
    def test_pagination(self, mock_get):
        # First page: 720 candles
        page1_candles = [
            _make_candle(1700000000 + i * 60) for i in range(720)
        ]
        page1_last = 1700000000 + 720 * 60

        # Second page: 3 candles
        page2_candles = [
            _make_candle(page1_last + i * 60) for i in range(3)
        ]
        page2_last = page1_last + 3 * 60

        mock_get.side_effect = [
            _mock_response(page1_candles, last=page1_last),
            _mock_response(page2_candles, last=page2_last),
            _mock_response([], last=page2_last),
        ]

        df = fetch_ohlcv_history("DOGE/USD", interval=1, since=1700000000)

        assert len(df) == 723
        assert mock_get.call_count == 3


class TestErrorHandling:
    """test_error_handling: Kraken returns an error array."""

    @patch("research.ohlcv_history.httpx.get")
    def test_error_handling(self, mock_get):
        mock_get.return_value = _mock_error_response(
            ["EGeneral:Too many requests"]
        )

        with pytest.raises(OHLCVHistoryError, match="Kraken API error"):
            fetch_ohlcv_history("DOGE/USD", interval=60, since=1700000000)


class TestTimestampsAreIntegers:
    """test_timestamps_are_integers: timestamp column has int dtype."""

    @patch("research.ohlcv_history.httpx.get")
    def test_timestamps_are_integers(self, mock_get):
        candles = [
            _make_candle(1700000000),
            _make_candle(1700003600),
        ]
        mock_get.side_effect = [
            _mock_response(candles, last=1700003600),
            _mock_response([], last=1700003600),
        ]

        df = fetch_ohlcv_history("DOGE/USD", interval=60, since=1700000000)

        assert df["timestamp"].dtype in ("int64", "int32")
        assert df["timestamp"].iloc[0] == 1700000000
