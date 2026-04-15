"""Tests for scripts/doge_snapshot.py -- Spec 36.

22 test cases covering:
  - Indicator helpers (pure math, no network)
  - Snapshot builder with monkeypatched _fetch
  - Renderers (human + JSON)
  - Logger
"""
from __future__ import annotations

import json
import math

import pytest

import scripts.doge_snapshot as ds

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_synthetic_bars(n: int = 200, start: float = 0.1, step: float = 0.0001) -> list[dict]:
    """Generate synthetic ascending-price OHLCV bars."""
    return [{"close": str(start + i * step), "open": str(start + i * step),
             "high": str(start + i * step + 0.00005),
             "low": str(start + i * step - 0.00005),
             "volume": "10000"} for i in range(n)]


def _make_good_snapshot() -> dict:
    """Build a minimal but valid snapshot dict for renderer tests."""
    closes = [0.10 + i * 0.001 for i in range(200)]
    macd_data = ds._macd(closes)
    hist_run, hist_color = ds._hist_run(macd_data["hist"])
    cross = ds._macd_cross(macd_data["line"], macd_data["signal"])
    tf_entry = {
        "rsi": 55.0,
        "macd_line": macd_data["line"][-1],
        "macd_signal": macd_data["signal"][-1],
        "macd_cross": cross,
        "hist_run": hist_run,
        "hist_color": hist_color,
        "vol_pct": 0.5,
        "bar_count": 200,
    }
    return {
        "pair": "DOGE/USD",
        "timestamp_utc": "2026-04-14T14:23:01Z",
        "price": 0.12345,
        "change_24h_pct": 2.45,
        "change_24h_color": "green",
        "holdings": {
            "doge_qty": 250.5,
            "doge_value_usd": 30.92,
            "usd_cash": 19.08,
            "doge_pct": 62.0,
            "usd_pct": 38.0,
        },
        "timeframes": {
            "1m": tf_entry,
            "15m": tf_entry,
            "1h": tf_entry,
            "4h": tf_entry,
            "1d": tf_entry,
        },
        "errors": [],
    }


# ---------------------------------------------------------------------------
# Part A -- Unit tests for indicator helpers
# ---------------------------------------------------------------------------

def test_ema_series_length_and_first_value():
    """a. Input [10, 11, 12, 13], span 3 -> length 4, first value 10.0."""
    result = ds._ema_series([10.0, 11.0, 12.0, 13.0], span=3)
    assert len(result) == 4
    assert result[0] == 10.0


def test_ema_series_known_smoothing():
    """b. Compare against hand-computed reference for [1, 2, 3, 4, 5], span 3."""
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    span = 3
    alpha = 2.0 / (span + 1)  # 0.5
    # Hand-computed:
    # e[0] = 1.0
    # e[1] = 2*0.5 + 1*0.5 = 1.5
    # e[2] = 3*0.5 + 1.5*0.5 = 2.25
    # e[3] = 4*0.5 + 2.25*0.5 = 3.125
    # e[4] = 5*0.5 + 3.125*0.5 = 4.0625
    expected = [1.0, 1.5, 2.25, 3.125, 4.0625]
    result = ds._ema_series(values, span=span)
    assert len(result) == 5
    for r, e in zip(result, expected):
        assert abs(r - e) < 1e-9, f"Expected {e}, got {r}"


def test_rsi_wilder_neutral_on_flat_input():
    """c. 50 equal closes -> RSI = 50.0 (no gains/losses -> neutral fallback)."""
    closes = [0.1] * 50
    result = ds._rsi_wilder(closes)
    assert result == 50.0


def test_rsi_wilder_max_on_monotonic_up():
    """d. 50 strictly increasing closes -> RSI > 99."""
    closes = [float(i) for i in range(1, 51)]
    result = ds._rsi_wilder(closes)
    assert result > 99.0, f"Expected > 99, got {result}"


def test_rsi_wilder_min_on_monotonic_down():
    """e. 50 strictly decreasing closes -> RSI < 1."""
    closes = [float(50 - i) for i in range(50)]
    result = ds._rsi_wilder(closes)
    assert result < 1.0, f"Expected < 1, got {result}"


def test_rsi_wilder_short_input_returns_50():
    """f. 5 closes -> 50.0."""
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = ds._rsi_wilder(closes)
    assert result == 50.0


def test_macd_returns_three_series_of_same_length():
    """g. 60 closes in -> line, signal, hist each length 60."""
    closes = [float(i) * 0.01 + 1.0 for i in range(60)]
    result = ds._macd(closes)
    assert len(result["line"]) == 60
    assert len(result["signal"]) == 60
    assert len(result["hist"]) == 60


def test_hist_run_counts_trailing_same_sign():
    """h. Three cases: mixed green, mixed red, empty."""
    # [-1, -1, 1, 1, 1] -> (3, "g")
    count, color = ds._hist_run([-1.0, -1.0, 1.0, 1.0, 1.0])
    assert count == 3
    assert color == "g"

    # [1, -1, -1] -> (2, "r")
    count, color = ds._hist_run([1.0, -1.0, -1.0])
    assert count == 2
    assert color == "r"

    # Empty list -> (0, "-")
    count, color = ds._hist_run([])
    assert count == 0
    assert color == "-"


def test_macd_cross_up_down_none():
    """i. Three crafted inputs exercising each branch."""
    # Up cross: prev line below signal, curr above
    count_up = ds._macd_cross([-0.1, 0.1], [0.0, 0.0])
    assert count_up == "up"

    # Down cross: prev line above signal, curr below
    count_down = ds._macd_cross([0.1, -0.1], [0.0, 0.0])
    assert count_down == "down"

    # No cross: both same side
    count_none = ds._macd_cross([0.1, 0.2], [0.0, 0.0])
    assert count_none == "none"


def test_volatility_pct_zero_on_constant():
    """j. 50 equal closes -> 0.0 (stdev of identical log returns = 0)."""
    closes = [1.0] * 50
    result = ds._volatility_pct(closes)
    assert result == 0.0


def test_volatility_pct_positive_on_random_walk():
    """k. 50 closes with known returns, assert > 0 and finite."""
    import random
    rng = random.Random(42)
    closes = [1.0]
    for _ in range(49):
        closes.append(closes[-1] * (1 + rng.gauss(0, 0.01)))
    result = ds._volatility_pct(closes)
    assert result > 0.0
    assert math.isfinite(result)


def test_24h_change_pct_uses_25th_to_last_bar():
    """l. 30 closes; 25th-to-last is exactly half of latest -> ~100.0%."""
    closes = [1.0] * 30
    # closes[-25] is index 5, closes[-1] is index 29
    closes[5] = 0.5   # 25th-to-last
    closes[29] = 1.0  # latest
    result = ds._24h_change_pct(closes)
    assert abs(result - 100.0) < 1e-9, f"Expected ~100.0, got {result}"


# ---------------------------------------------------------------------------
# Part B -- Snapshot builder + renderer tests (monkeypatched _fetch)
# ---------------------------------------------------------------------------

def _make_ohlcv_response(n: int = 200) -> dict:
    bars = _make_synthetic_bars(n)
    return {"bars": bars}


def test_build_snapshot_calls_all_five_timeframes(monkeypatch):
    """m. Assert all intervals 1, 15, 60, 240, 1440 were called."""
    called_intervals: list[str] = []

    def fake_fetch(endpoint: str, bot_url: str, method: str = "GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {
                "balances": [
                    {"asset": "DOGE", "available": "100.0", "held": "0.5"},
                    {"asset": "USD",  "available": "50.0",  "held": "0"},
                ],
                "count": 2,
            }
        if "/api/ohlcv/" in endpoint:
            # Extract interval from query string
            interval = endpoint.split("interval=")[1].split("&")[0]
            called_intervals.append(interval)
            return _make_ohlcv_response()
        return {"error": "unexpected endpoint"}

    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snapshot = ds.build_snapshot("http://127.0.0.1:58392", "DOGE/USD")

    assert set(called_intervals) == {"1", "15", "60", "240", "1440"}
    assert len(snapshot["timeframes"]) == 5
    for tf in ("1m", "15m", "1h", "4h", "1d"):
        assert snapshot["timeframes"][tf] is not None


def test_build_snapshot_handles_per_tf_failure(monkeypatch):
    """n. _fetch fails for 4h only; 4h is None, error logged, others populated."""

    def fake_fetch(endpoint: str, bot_url: str, method: str = "GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {
                "balances": [
                    {"asset": "DOGE", "available": "100.0", "held": "0.5"},
                    {"asset": "USD",  "available": "50.0",  "held": "0"},
                ],
                "count": 2,
            }
        if "/api/ohlcv/" in endpoint:
            if "interval=240" in endpoint:
                return {"error": "boom"}
            return _make_ohlcv_response()
        return {"error": "unexpected"}

    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snapshot = ds.build_snapshot("http://127.0.0.1:58392", "DOGE/USD")

    assert snapshot["timeframes"]["4h"] is None
    assert any("4h" in e for e in snapshot["errors"])
    # Other TFs should still be populated
    for tf in ("1m", "15m", "1h", "1d"):
        assert snapshot["timeframes"][tf] is not None


def test_build_snapshot_handles_balances_failure(monkeypatch):
    """o. Balances fetch fails; holdings=None, error logged. OHLCV TFs still populated."""

    def fake_fetch(endpoint: str, bot_url: str, method: str = "GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {"error": "network timeout"}
        if "/api/ohlcv/" in endpoint:
            return _make_ohlcv_response()
        return {"error": "unexpected"}


    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snapshot = ds.build_snapshot("http://127.0.0.1:58392", "DOGE/USD")

    assert snapshot["holdings"] is None
    assert any("balances" in e for e in snapshot["errors"])
    # OHLCV TFs should still be populated
    for tf in ("1m", "15m", "1h", "4h", "1d"):
        assert snapshot["timeframes"][tf] is not None


def test_render_human_runs_without_color():
    """p. Happy-path snapshot, color=False: contains DOGE/USD, all 5 TF labels, no ANSI."""
    snap = _make_good_snapshot()
    result = ds.render_human(snap, color=False)

    assert "DOGE/USD" in result
    for tf in ("1m", "15m", "1h", "4h", "1d"):
        assert tf in result
    assert "\x1b" not in result


def test_render_human_with_color_includes_ansi():
    """q. color=True: at least one ANSI escape present."""
    snap = _make_good_snapshot()
    # Make sure there's a green or red element to trigger color
    snap["change_24h_color"] = "green"
    result = ds.render_human(snap, color=True)
    assert "\x1b[" in result


def test_render_human_handles_missing_holdings():
    """r. holdings=None -> 'Holdings: unavailable', no raise."""
    snap = _make_good_snapshot()
    snap["holdings"] = None
    result = ds.render_human(snap, color=False)
    assert "Holdings: unavailable" in result


def test_render_human_handles_failed_tf():
    """s. timeframes["1m"] = None -> row shows '1m' and 'FETCH FAILED'."""
    snap = _make_good_snapshot()
    snap["timeframes"]["1m"] = None
    result = ds.render_human(snap, color=False)
    assert "1m" in result
    assert "FETCH FAILED" in result


def test_render_json_emits_valid_json():
    """t. json.loads(render_json(snap)) round-trips."""
    snap = _make_good_snapshot()
    json_str = ds.render_json(snap)
    parsed = json.loads(json_str)
    assert parsed["pair"] == "DOGE/USD"
    assert "timeframes" in parsed


# ---------------------------------------------------------------------------
# Part C -- Logger tests
# ---------------------------------------------------------------------------

def test_log_decision_posts_correct_payload(monkeypatch):
    """u. Assert correct payload structure POSTed to /api/memory."""
    captured_payloads: list[dict] = []

    def fake_fetch(endpoint: str, bot_url: str, method: str = "GET", data=None):
        captured_payloads.append({"endpoint": endpoint, "method": method, "data": data})
        return {"id": 1, "status": "stored"}

    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snap = _make_good_snapshot()
    result = ds.log_decision("http://127.0.0.1:58392", snap, "DOGE", note=None)

    assert len(captured_payloads) == 1
    payload = captured_payloads[0]["data"]
    assert payload["category"] == "doge_snapshot"
    assert payload["pair"] == "DOGE/USD"
    assert payload["content"]["decision"] == "DOGE"
    assert payload["content"]["schema_version"] == "v1"
    assert payload["content"]["snapshot"] == snap


def test_log_decision_rejects_invalid_value(monkeypatch):
    """v. decision='MAYBE' -> raises ValueError."""
    def fake_fetch(endpoint, bot_url, method="GET", data=None):
        return {"id": 1, "status": "stored"}

    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snap = _make_good_snapshot()

    with pytest.raises(ValueError):
        ds.log_decision("http://127.0.0.1:58392", snap, "MAYBE", note=None)


def test_log_decision_passes_through_note(monkeypatch):
    """w. note='vibes' ends up at content['note']=='vibes'. note=None -> null."""
    captured_payloads: list[dict] = []

    def fake_fetch(endpoint: str, bot_url: str, method: str = "GET", data=None):
        captured_payloads.append(data)
        return {"id": 1, "status": "stored"}

    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snap = _make_good_snapshot()

    # With note
    ds.log_decision("http://127.0.0.1:58392", snap, "SPLIT", note="vibes")
    assert captured_payloads[-1]["content"]["note"] == "vibes"

    # Without note (None)
    ds.log_decision("http://127.0.0.1:58392", snap, "USD", note=None)
    assert captured_payloads[-1]["content"]["note"] is None


# ---------------------------------------------------------------------------
# Part D -- Regression tests added by Codex review (Issues 2-6)
# ---------------------------------------------------------------------------

def test_build_snapshot_uses_available_plus_held(monkeypatch):
    """Regression #2: held quantity must be added to available, not ignored."""
    def fake_fetch(endpoint, bot_url, method="GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {
                "balances": [
                    {"asset": "DOGE", "available": "60", "held": "40"},
                    {"asset": "USD",  "available": "10", "held": "5"},
                ],
                "count": 2,
            }
        if "/api/ohlcv/" in endpoint:
            return _make_ohlcv_response()
        return {"error": "unexpected"}
    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snap = ds.build_snapshot("http://test", "DOGE/USD")
    assert snap["holdings"]["doge_qty"] == 100.0  # 60 + 40
    assert snap["holdings"]["usd_cash"] == 15.0   # 10 + 5


def test_build_snapshot_marks_tf_with_insufficient_bars(monkeypatch):
    """Regression #3: Timeframes with < 35 bars are marked None with an error."""
    def fake_fetch(endpoint, bot_url, method="GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {"balances": [{"asset": "DOGE", "available": "1", "held": "0"},
                                  {"asset": "USD", "available": "1", "held": "0"}], "count": 2}
        if "interval=1&" in endpoint:
            return {"bars": _make_synthetic_bars(20)}  # too few
        if "/api/ohlcv/" in endpoint:
            return _make_ohlcv_response()
        return {"error": "unexpected"}
    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    snap = ds.build_snapshot("http://test", "DOGE/USD")
    assert snap["timeframes"]["1m"] is None
    assert any("insufficient" in e for e in snap["errors"])
    assert snap["timeframes"]["15m"] is not None


def test_main_skips_log_when_all_tfs_failed(monkeypatch, capsys):
    """Regression #4: When every TF fetch fails, --log must not write a memory row."""
    log_called: list = []

    def fake_fetch(endpoint, bot_url, method="GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {"balances": [], "count": 0}
        if "/api/ohlcv/" in endpoint:
            return {"error": "boom"}
        if "/api/memory" in endpoint:
            log_called.append(data)
            return {"id": 1, "status": "stored"}
        return {"error": "unexpected"}

    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    with pytest.raises(SystemExit) as exc_info:
        ds.main(["--bot-url", "http://test", "--log", "DOGE", "--no-color"])
    assert exc_info.value.code == 1
    assert log_called == []


def test_main_log_failure_on_zero_id(monkeypatch, capsys):
    """Regression #5: When /api/memory returns id=0, main prints log failed and exits 1."""
    def fake_fetch(endpoint, bot_url, method="GET", data=None):
        if "/api/exchange-balances" in endpoint:
            return {"balances": [{"asset": "DOGE", "available": "1", "held": "0"},
                                  {"asset": "USD", "available": "1", "held": "0"}], "count": 2}
        if "/api/ohlcv/" in endpoint:
            return _make_ohlcv_response()
        if "/api/memory" in endpoint:
            return {"id": 0, "status": "stored"}
        return {"error": "unexpected"}
    monkeypatch.setattr(ds, "_fetch", fake_fetch)
    with pytest.raises(SystemExit) as exc_info:
        ds.main(["--bot-url", "http://test", "--log", "SPLIT", "--no-color"])
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "log failed" in captured.out


def test_main_rejects_non_doge_usd_pair(capsys):
    """Regression #6: --pair other than DOGE/USD exits with code 2."""
    with pytest.raises(SystemExit) as exc_info:
        ds.main(["--pair", "BTC/USD"])
    assert exc_info.value.code == 2
