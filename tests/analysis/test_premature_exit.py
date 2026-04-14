from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from analysis.premature_exit import (
    _aggregate_1h_to_4h,
    _classify,
    detect_premature_exits,
)
from persistence.cc_memory import CCMemory
from research.ohlcv_cryptocompare import CryptoCompareError


def _aligned_start_ts() -> int:
    return 1_704_067_200  # 2024-01-01 00:00:00 UTC


def _make_1h_bars(start_ts: int, count: int, *, start_price: int = 100, step: int = 0) -> pd.DataFrame:
    rows: list[dict] = []
    for i in range(count):
        price = Decimal(str(start_price + (i * step)))
        rows.append({
            "timestamp": start_ts + (i * 3600),
            "open": price,
            "high": price + Decimal("1"),
            "low": price - Decimal("1"),
            "close": price,
            "volume": Decimal("1"),
        })
    return pd.DataFrame(rows)


def _outcome(
    outcome_id: int,
    pair: str,
    exit_price: str,
    exit_reason: str,
    closed_at_ts: int,
    *,
    net_pnl: str = "2.50",
) -> dict:
    return {
        "id": outcome_id,
        "pair": pair,
        "exit_price": exit_price,
        "net_pnl": net_pnl,
        "exit_reason": exit_reason,
        "closed_at": datetime.fromtimestamp(closed_at_ts, tz=timezone.utc).isoformat(),
    }


def test_classify_premature_when_above_both_emas():
    assert _classify(
        Decimal("100"),
        Decimal("95"),
        Decimal("90"),
        "timer",
    ) is True


def test_classify_not_premature_when_below_ema10():
    assert _classify(
        Decimal("92"),
        Decimal("95"),
        Decimal("90"),
        "timer",
    ) is False


def test_classify_not_premature_when_stop_loss():
    assert _classify(
        Decimal("100"),
        Decimal("95"),
        Decimal("90"),
        "stop_loss",
    ) is False


def test_aggregate_1h_to_4h_boundary():
    df_1h = _make_1h_bars(_aligned_start_ts(), 8, start_price=100, step=1)

    df_4h = _aggregate_1h_to_4h(df_1h)

    assert len(df_4h) == 2
    assert df_4h.iloc[0]["bucket_start"] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert df_4h.iloc[0]["open"] == Decimal("100")
    assert df_4h.iloc[0]["high"] == Decimal("104")
    assert df_4h.iloc[0]["low"] == Decimal("99")
    assert df_4h.iloc[0]["close"] == Decimal("103")


def test_aggregate_1h_to_4h_drops_partial():
    df_1h = _make_1h_bars(_aligned_start_ts(), 10, start_price=100, step=0)

    df_4h = _aggregate_1h_to_4h(df_1h)

    assert len(df_4h) == 2
    assert list(df_4h["bucket_start"]) == [
        pd.Timestamp("2024-01-01T00:00:00Z"),
        pd.Timestamp("2024-01-01T04:00:00Z"),
    ]


def test_detect_writes_memory_for_premature_exit():
    start_ts = _aligned_start_ts()
    bars = _make_1h_bars(start_ts, 120, start_price=100, step=0)
    closed_at_ts = start_ts + (120 * 3600)

    def fetch_bars(pair, interval=60, since=None, until=None):
        assert pair == "SOL/USD"
        assert interval == 60
        assert since is not None
        assert until == closed_at_ts
        return bars

    cc_memory = CCMemory(":memory:")
    result = detect_premature_exits(
        lookback_days=30,
        cc_memory=cc_memory,
        trade_outcomes=[_outcome(1, "SOL/USD", "105", "timer", closed_at_ts)],
        fetch_bars=fetch_bars,
    )

    assert result == {"scanned": 1, "flagged": 1, "skipped": 0, "errors": 0}
    rows = cc_memory.query(category="premature_exit", hours=24 * 365, limit=10)
    assert len(rows) == 1
    assert rows[0]["pair"] == "SOL/USD"
    assert rows[0]["importance"] == 0.7
    assert rows[0]["content"] == {
        "trade_outcome_id": 1,
        "closed_at": datetime.fromtimestamp(closed_at_ts, tz=timezone.utc).isoformat(),
        "exit_reason": "timer",
        "exit_price": "105",
        "ema10_4h": "100.0",
        "ema20_4h": "100.0",
        "net_pnl": "2.50",
        "rule_version": "v1",
    }


def test_detect_idempotent_on_rerun():
    start_ts = _aligned_start_ts()
    bars = _make_1h_bars(start_ts, 120, start_price=100, step=0)
    closed_at_ts = start_ts + (120 * 3600)

    def fetch_bars(pair, interval=60, since=None, until=None):
        return bars

    outcome = _outcome(1, "SOL/USD", "105", "timer", closed_at_ts)
    cc_memory = CCMemory(":memory:")

    first = detect_premature_exits(
        lookback_days=30,
        cc_memory=cc_memory,
        trade_outcomes=[outcome],
        fetch_bars=fetch_bars,
    )
    second = detect_premature_exits(
        lookback_days=30,
        cc_memory=cc_memory,
        trade_outcomes=[outcome],
        fetch_bars=fetch_bars,
    )

    assert first == {"scanned": 1, "flagged": 1, "skipped": 0, "errors": 0}
    assert second == {"scanned": 1, "flagged": 0, "skipped": 1, "errors": 0}
    assert len(cc_memory.query(category="premature_exit", hours=24 * 365, limit=10)) == 1


def test_detect_skips_exits_with_insufficient_history():
    start_ts = _aligned_start_ts()
    bars = _make_1h_bars(start_ts, 20, start_price=100, step=0)
    closed_at_ts = start_ts + (20 * 3600)

    def fetch_bars(pair, interval=60, since=None, until=None):
        return bars

    cc_memory = CCMemory(":memory:")
    result = detect_premature_exits(
        lookback_days=30,
        cc_memory=cc_memory,
        trade_outcomes=[_outcome(1, "SOL/USD", "105", "timer", closed_at_ts)],
        fetch_bars=fetch_bars,
    )

    assert result == {"scanned": 1, "flagged": 0, "skipped": 1, "errors": 0}
    assert cc_memory.query(category="premature_exit", hours=24 * 365, limit=10) == []


def test_detect_continues_on_per_pair_error():
    start_ts = _aligned_start_ts()
    bars = _make_1h_bars(start_ts, 120, start_price=100, step=0)
    closed_at_ts = start_ts + (120 * 3600)

    def fetch_bars(pair, interval=60, since=None, until=None):
        if pair == "BAD/USD":
            raise CryptoCompareError("boom")
        return bars

    cc_memory = CCMemory(":memory:")
    result = detect_premature_exits(
        lookback_days=30,
        cc_memory=cc_memory,
        trade_outcomes=[
            _outcome(1, "BAD/USD", "105", "timer", closed_at_ts, net_pnl="0"),
            _outcome(2, "SOL/USD", "105", "timer", closed_at_ts, net_pnl="0"),
        ],
        fetch_bars=fetch_bars,
    )

    assert result == {"scanned": 2, "flagged": 1, "skipped": 0, "errors": 1}
    rows = cc_memory.query(category="premature_exit", hours=24 * 365, limit=10)
    assert len(rows) == 1
    assert rows[0]["content"]["trade_outcome_id"] == 2

