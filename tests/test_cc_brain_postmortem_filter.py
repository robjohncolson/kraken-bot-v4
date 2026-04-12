from __future__ import annotations

import scripts.cc_brain as cc_brain


def _sample_outcomes() -> list[dict]:
    return [
        {"pair": "AERO/USD", "net_pnl": "1.25", "anomaly_flag": None},
        {
            "pair": "USDT/USD",
            "net_pnl": "-15.00",
            "anomaly_flag": "stablecoin_unit_mismatch",
        },
        {"pair": "AVAX/USD", "net_pnl": "-0.50", "anomaly_flag": None},
    ]


def test_postmortem_excludes_anomaly_rows(monkeypatch):
    logs: list[str] = []
    losers_seen: list[list[dict]] = []

    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        assert method == "GET"
        assert data is None
        assert endpoint == "/api/trade-outcomes?lookback_days=7"
        return {"outcomes": _sample_outcomes()}

    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)
    monkeypatch.setattr(
        cc_brain,
        "immediate_postmortem",
        lambda outcomes, log_fn: losers_seen.append(outcomes) or [],
    )
    monkeypatch.setattr(cc_brain, "deep_postmortem", lambda log_fn: None)
    monkeypatch.setattr(cc_brain, "self_tune", lambda outcomes, analyses, log_fn: None)

    recent_trades = cc_brain.run_postmortem_step([], logs.append)

    assert len(recent_trades) == 2
    assert "Last 7 days: 2 trades, 1 wins, P&L=$0.7500" in logs
    assert "Anomalies (excluded from stats): 1 rows" in logs
    assert (
        "  ANOMALY: USDT/USD net_pnl=$-15.0000 "
        "(anomaly_flag=stablecoin_unit_mismatch)"
    ) in logs
    assert losers_seen == [[{"pair": "AVAX/USD", "net_pnl": "-0.50", "anomaly_flag": None}]]


def test_postmortem_self_tune_uses_filtered_pnl(monkeypatch):
    captured: dict[str, float | int] = {}

    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        assert method == "GET"
        assert data is None
        assert endpoint == "/api/trade-outcomes?lookback_days=7"
        return {"outcomes": _sample_outcomes()}

    def fake_self_tune(outcomes: list[dict], analyses: list[dict], log_fn) -> None:
        captured["count"] = len(outcomes)
        captured["total_pnl"] = sum(float(t.get("net_pnl", 0) or 0) for t in outcomes)

    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)
    monkeypatch.setattr(cc_brain, "immediate_postmortem", lambda outcomes, log_fn: [])
    monkeypatch.setattr(cc_brain, "deep_postmortem", lambda log_fn: None)
    monkeypatch.setattr(cc_brain, "self_tune", fake_self_tune)

    cc_brain.run_postmortem_step([], lambda msg: None)

    assert captured["count"] == 2
    assert captured["total_pnl"] == 0.75
    assert captured["total_pnl"] != sum(
        float(t.get("net_pnl", 0) or 0) for t in _sample_outcomes()
    )
