from __future__ import annotations

import time
from concurrent.futures import Future
from decimal import Decimal

import pandas as pd

from core.config import Settings, load_settings
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    MarketRegime,
    OrderSide,
)
from exchange.client import KrakenClient
from trading import pair_scanner as pair_scanner_module
from trading.pair_scanner import PairScanner


REQUIRED_ENV = {
    "KRAKEN_API_KEY": "kraken-key",
    "KRAKEN_API_SECRET": "kraken-secret",
}


class FakeClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def now(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class FakeTechnicalSource:
    def __init__(self, beliefs: dict[str, BeliefSnapshot], *, min_bars: int = 40) -> None:
        self._beliefs = beliefs
        self.min_bars = min_bars
        self.calls: list[str] = []

    def analyze(self, pair: str, bars: pd.DataFrame) -> BeliefSnapshot:
        self.calls.append(pair)
        return self._beliefs[pair]


def test_discover_usd_spot_pairs_normalizes_and_caches_results() -> None:
    calls = {"count": 0}
    clock = FakeClock()

    def asset_pairs_fetcher(client: KrakenClient, timeout_sec: float):
        del client, timeout_sec
        calls["count"] += 1
        return {
            "XXBTZUSD": {
                "aclass_base": "currency",
                "aclass_quote": "currency",
                "quote": "ZUSD",
                "wsname": "XBT/USD",
            },
            "XDGUSD": {
                "aclass_base": "currency",
                "aclass_quote": "currency",
                "quote": "USD",
                "altname": "XDGUSD",
            },
            "XETHXXBT": {
                "aclass_base": "currency",
                "aclass_quote": "currency",
                "quote": "XXBT",
                "wsname": "ETH/XBT",
            },
            "XXBTZUSD.d": {
                "aclass_base": "currency",
                "aclass_quote": "currency",
                "quote": "ZUSD",
                "wsname": "XBT/USD.d",
            },
        }

    scanner = PairScanner(
        client=_client(),
        settings=_settings(SCANNER_PAIR_DISCOVERY_TTL_SEC="60"),
        asset_pairs_fetcher=asset_pairs_fetcher,
        time_source=clock.now,
    )

    first = scanner.discover_usd_spot_pairs()
    second = scanner.discover_usd_spot_pairs()
    clock.advance(61.0)
    third = scanner.discover_usd_spot_pairs()

    assert first == ("BTC/USD", "DOGE/USD")
    assert second == first
    assert third == first
    assert calls["count"] == 2


def test_scan_bull_candidates_filters_non_bullish_and_ranks_by_confidence() -> None:
    beliefs = {
        "BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.91),
        "ETH/USD": _belief("ETH/USD", BeliefDirection.BULLISH, 0.77),
        "DOGE/USD": _belief("DOGE/USD", BeliefDirection.BEARISH, 0.33),
    }
    source = FakeTechnicalSource(beliefs)
    prices = {
        "BTC/USD": _bars_from_close(_up_saw_closes()),
        "ETH/USD": _bars_from_close(_up_saw_closes(last_close=111.0)),
        "DOGE/USD": _bars_from_close(_up_saw_closes(last_close=95.0)),
    }

    scanner = PairScanner(
        client=_client(),
        settings=_settings(),
        technical_source=source,
        asset_pairs_fetcher=lambda client, timeout_sec: {
            "XXBTZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "XBT/USD"},
            "XETHZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "ETH/USD"},
            "XDGUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "USD", "altname": "XDGUSD"},
        },
        ohlcv_fetcher=lambda pair, **kwargs: prices[pair],
    )

    candidates = scanner.scan_bull_candidates()

    assert tuple(candidate.pair for candidate in candidates) == ("BTC/USD", "ETH/USD")
    assert tuple(candidate.confidence for candidate in candidates) == (0.91, 0.77)
    assert candidates[0].reference_price_hint == Decimal("101")
    assert candidates[1].reference_price_hint == Decimal("111")
    assert all(candidate.estimated_peak_hours > 0 for candidate in candidates)


def test_scan_bull_candidates_uses_configured_max_concurrency(monkeypatch) -> None:
    captured: dict[str, int] = {}

    class FakeExecutor:
        def __init__(self, *, max_workers: int) -> None:
            captured["max_workers"] = max_workers

        def submit(self, fn, *args, **kwargs):
            future: Future = Future()
            future.set_result(fn(*args, **kwargs))
            return future

        def shutdown(self, wait: bool, cancel_futures: bool) -> None:
            del wait, cancel_futures

    monkeypatch.setattr(pair_scanner_module, "ThreadPoolExecutor", FakeExecutor)

    scanner = PairScanner(
        client=_client(),
        settings=_settings(SCANNER_MAX_CONCURRENCY="2"),
        technical_source=FakeTechnicalSource(
            {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.8)},
        ),
        asset_pairs_fetcher=lambda client, timeout_sec: {
            "XXBTZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "XBT/USD"},
        },
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_up_saw_closes()),
    )

    scanner.scan_bull_candidates()

    assert captured["max_workers"] == 2


def test_scan_bull_candidates_returns_empty_tuple_on_timeout() -> None:
    scanner = PairScanner(
        client=_client(),
        settings=_settings(SCANNER_TIMEOUT_SEC="0.01", SCANNER_MAX_CONCURRENCY="1"),
        technical_source=FakeTechnicalSource(
            {
                "BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.8),
                "ETH/USD": _belief("ETH/USD", BeliefDirection.BULLISH, 0.7),
            },
        ),
        asset_pairs_fetcher=lambda client, timeout_sec: {
            "XXBTZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "XBT/USD"},
            "XETHZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "ETH/USD"},
        },
        ohlcv_fetcher=_sleepy_ohlcv_fetcher,
    )

    started = time.perf_counter()
    candidates = scanner.scan_bull_candidates()
    elapsed = time.perf_counter() - started

    assert candidates == ()
    assert elapsed < 0.10


def test_scan_bull_candidates_derives_peak_hours_from_bullish_extension() -> None:
    closes = [
        100.0, 98.58009038398662, 98.98509011372099, 99.30410536698365, 99.53196421107828,
        99.20559243904675, 98.81601226005232, 100.25756221199413, 98.86673832482859,
        97.43164785439366, 98.81474169511249, 97.86965751930481, 96.7413430125781,
        95.87307254223803, 96.77531231330057, 98.08621978923432, 96.65456751624029,
        96.43142401214074, 95.23592467026324, 94.51568433964174, 93.6781721535907,
        94.11894931309666, 93.66983121528627, 92.71078491987532, 92.72169443550499,
        91.33983055675907, 90.14259428032597, 91.60729972649347, 90.70536709789465,
        90.28103300182946, 90.97582792050554, 91.99080761608579, 93.24625380207178,
        92.25452762036419, 92.77244931108335, 94.1720960202129, 92.8462488516924,
        93.37485420459053, 94.41112798569539, 93.93806560893114,
    ]
    scanner = PairScanner(
        client=_client(),
        settings=_settings(),
        technical_source=FakeTechnicalSource(
            {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.85)},
        ),
        asset_pairs_fetcher=lambda client, timeout_sec: {
            "XXBTZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "XBT/USD"},
        },
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(closes),
    )

    candidates = scanner.scan_bull_candidates()

    assert len(candidates) == 1
    assert candidates[0].estimated_peak_hours == 24


def test_scan_bull_candidates_reduces_peak_hours_when_rsi_is_overbought() -> None:
    scanner = PairScanner(
        client=_client(),
        settings=_settings(),
        technical_source=FakeTechnicalSource(
            {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.85)},
        ),
        asset_pairs_fetcher=lambda client, timeout_sec: {
            "XXBTZUSD": {"aclass_base": "currency", "aclass_quote": "currency", "quote": "ZUSD", "wsname": "XBT/USD"},
        },
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close([100 + (i * 0.8) for i in range(40)]),
    )

    candidates = scanner.scan_bull_candidates()

    assert len(candidates) == 1
    assert candidates[0].estimated_peak_hours == 12


def test_scan_rotation_pair_rejects_low_volume() -> None:
    source = FakeTechnicalSource(
        {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.8)},
    )
    bars = _bars_from_close(
        [4.0 + (i * 0.01) for i in range(40)],
        volume=1.0,
    )
    scanner = PairScanner(
        client=_client(),
        settings=_settings(),
        technical_source=source,
        ohlcv_fetcher=lambda pair, **kwargs: bars,
    )

    result = scanner._scan_rotation_pair(
        "BTC/USD",
        "USD",
        "BTC",
        OrderSide.BUY,
        None,
    )

    assert result is None
    assert source.calls == []


def test_scan_rotation_pair_rejects_wide_spread() -> None:
    source = FakeTechnicalSource(
        {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.8)},
    )
    bars = _bars_from_close(
        _up_saw_closes(),
        volume=5000.0,
        high_mult=1.05,
        low_mult=0.95,
    )
    scanner = PairScanner(
        client=_client(),
        settings=_settings(),
        technical_source=source,
        ohlcv_fetcher=lambda pair, **kwargs: bars,
    )

    result = scanner._scan_rotation_pair(
        "BTC/USD",
        "USD",
        "BTC",
        OrderSide.BUY,
        None,
    )

    assert result is None
    assert source.calls == []


def test_scan_rotation_pair_accepts_liquid_pair() -> None:
    source = FakeTechnicalSource(
        {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.8)},
    )
    bars = _bars_from_close(_up_saw_closes(), volume=5000.0)
    scanner = PairScanner(
        client=_client(),
        settings=_settings(),
        technical_source=source,
        ohlcv_fetcher=lambda pair, **kwargs: bars,
    )

    result = scanner._scan_rotation_pair(
        "BTC/USD",
        "USD",
        "BTC",
        OrderSide.BUY,
        None,
    )

    assert result is not None
    assert result.pair == "BTC/USD"
    assert source.calls == ["BTC/USD"]


def _settings(**overrides: str) -> Settings:
    env = {
        **REQUIRED_ENV,
        "SCANNER_PAIR_DISCOVERY_TTL_SEC": "3600",
        "SCANNER_MAX_CONCURRENCY": "4",
        "SCANNER_TIMEOUT_SEC": "15.0",
        **overrides,
    }
    return load_settings(env)


def _client() -> KrakenClient:
    return KrakenClient(api_key="key", api_secret="secret")


def _belief(pair: str, direction: BeliefDirection, confidence: float) -> BeliefSnapshot:
    return BeliefSnapshot(
        pair=pair,
        direction=direction,
        confidence=confidence,
        regime=MarketRegime.TRENDING,
        sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
    )


def _bars_from_close(
    closes: list[float],
    *,
    volume: float = 1000.0,
    high_mult: float = 1.005,
    low_mult: float = 0.995,
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": close * 0.99,
                "high": close * high_mult,
                "low": close * low_mult,
                "close": close,
                "volume": volume,
            }
            for close in closes
        ]
    )


def _up_saw_closes(*, last_close: float = 101.0) -> list[float]:
    closes = [
        80, 82, 81, 83, 82, 84, 83, 85, 84, 86,
        85, 87, 86, 88, 87, 89, 88, 90, 89, 91,
        90, 92, 91, 93, 92, 94, 93, 95, 94, 96,
        95, 97, 96, 98, 97, 99, 98, 100, 99, 101,
    ]
    closes[-1] = last_close
    return closes


def _sleepy_ohlcv_fetcher(pair: str, **kwargs) -> pd.DataFrame:
    del pair, kwargs
    time.sleep(0.05)
    return _bars_from_close(_up_saw_closes())


# ---------------------------------------------------------------------------
# Phase 8A: 4H trend gate tests
# ---------------------------------------------------------------------------


class SequentialFakeSource:
    """Returns beliefs in order of calls (1st call = beliefs[0], 2nd = beliefs[1], ...)."""

    def __init__(self, beliefs: list[BeliefSnapshot], *, min_bars: int = 40) -> None:
        self._beliefs = beliefs
        self._idx = 0
        self.min_bars = min_bars

    def analyze(self, pair: str, bars: pd.DataFrame) -> BeliefSnapshot:
        result = self._beliefs[self._idx % len(self._beliefs)]
        self._idx += 1
        return result


def test_scan_4h_aligned_boosts_confidence() -> None:
    """1H bullish (0.67) + 4H bullish → confidence boosted by 1.15x."""
    source = SequentialFakeSource([
        _belief("BTC/USD", BeliefDirection.BULLISH, 0.67),  # 1H
        _belief("BTC/USD", BeliefDirection.BULLISH, 0.83),  # 4H (same direction)
    ])
    bars = _bars_from_close(_up_saw_closes(), volume=5000.0)
    scanner = PairScanner(
        client=_client(),
        settings=_settings(MTF_4H_GATE_ENABLED="true"),
        technical_source=source,
        ohlcv_fetcher=lambda pair, **kwargs: bars,
    )

    result = scanner._scan_rotation_pair("BTC/USD", "USD", "BTC", OrderSide.BUY, None)

    assert result is not None
    assert abs(result.confidence - 0.67 * 1.15) < 1e-9


def test_scan_4h_counter_penalizes_confidence() -> None:
    """1H bullish (0.67) + 4H bearish → confidence * 0.3 = 0.20, likely below threshold."""
    source = SequentialFakeSource([
        _belief("BTC/USD", BeliefDirection.BULLISH, 0.67),  # 1H
        _belief("BTC/USD", BeliefDirection.BEARISH, 0.83),  # 4H (opposing)
    ])
    bars = _bars_from_close(_up_saw_closes(), volume=5000.0)
    scanner = PairScanner(
        client=_client(),
        settings=_settings(MTF_4H_GATE_ENABLED="true"),
        technical_source=source,
        ohlcv_fetcher=lambda pair, **kwargs: bars,
    )

    result = scanner._scan_rotation_pair("BTC/USD", "USD", "BTC", OrderSide.BUY, None)

    assert result is not None
    assert abs(result.confidence - 0.67 * 0.3) < 1e-9


def test_scan_4h_neutral_passthrough() -> None:
    """4H neutral → confidence unchanged (factor 1.0)."""
    source = SequentialFakeSource([
        _belief("BTC/USD", BeliefDirection.BULLISH, 0.67),  # 1H
        _belief("BTC/USD", BeliefDirection.NEUTRAL, 0.33),  # 4H neutral
    ])
    bars = _bars_from_close(_up_saw_closes(), volume=5000.0)
    scanner = PairScanner(
        client=_client(),
        settings=_settings(MTF_4H_GATE_ENABLED="true"),
        technical_source=source,
        ohlcv_fetcher=lambda pair, **kwargs: bars,
    )

    result = scanner._scan_rotation_pair("BTC/USD", "USD", "BTC", OrderSide.BUY, None)

    assert result is not None
    assert result.confidence == 0.67


def test_scan_4h_fetch_failure_graceful() -> None:
    """OHLCVFetchError on 4H → candidate produced with original confidence."""
    from exchange.ohlcv import OHLCVFetchError

    call_count = {"n": 0}

    def fetcher(pair: str, **kwargs) -> pd.DataFrame:
        call_count["n"] += 1
        if kwargs.get("interval") == 240:
            raise OHLCVFetchError("4H unavailable")
        return _bars_from_close(_up_saw_closes(), volume=5000.0)

    source = FakeTechnicalSource(
        {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.67)},
    )
    scanner = PairScanner(
        client=_client(),
        settings=_settings(MTF_4H_GATE_ENABLED="true"),
        technical_source=source,
        ohlcv_fetcher=fetcher,
    )

    result = scanner._scan_rotation_pair("BTC/USD", "USD", "BTC", OrderSide.BUY, None)

    assert result is not None
    assert result.confidence == 0.67


def test_scan_4h_gate_disabled() -> None:
    """MTF_4H_GATE_ENABLED=False → no 4H fetch, original confidence."""
    call_intervals: list[int] = []

    def fetcher(pair: str, **kwargs) -> pd.DataFrame:
        call_intervals.append(kwargs.get("interval", 60))
        return _bars_from_close(_up_saw_closes(), volume=5000.0)

    source = FakeTechnicalSource(
        {"BTC/USD": _belief("BTC/USD", BeliefDirection.BULLISH, 0.67)},
    )
    scanner = PairScanner(
        client=_client(),
        settings=_settings(MTF_4H_GATE_ENABLED="false"),
        technical_source=source,
        ohlcv_fetcher=fetcher,
    )

    result = scanner._scan_rotation_pair("BTC/USD", "USD", "BTC", OrderSide.BUY, None)

    assert result is not None
    assert result.confidence == 0.67
    assert 240 not in call_intervals
