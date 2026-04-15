from __future__ import annotations

import pytest

import scripts.cc_brain as cc_brain


def _analysis(pair: str, price: float) -> dict:
    return {
        "pair": pair,
        "price": price,
        "regime": "trending",
        "trade_gate": 1.0,
        "regime_probs": {"volatile": 0.1},
        "rsi_1h": 25.0,
        "trend_1h": "UP",
        "trend_4h": "UP",
        "ema7_1h": price,
        "ema26_1h": price,
        "kronos_direction": "bullish",
        "kronos_pct": 2.0,
        "kronos_volatility": 3.0,
        "timesfm_direction": "bullish",
        "timesfm_confidence": 1.0,
    }


def _run_entry_cycle(
    monkeypatch,
    tmp_path,
    analyses: list[dict],
    scores: dict[str, float],
    exchange_balances: list[dict],
    discovered_pairs: list[dict] | None = None,
    permission_memories: list[dict] | None = None,
    insufficient_memories: list[dict] | None = None,
):
    memory_posts: list[dict] = []
    analysis_map = {analysis["pair"]: dict(analysis) for analysis in analyses}
    if discovered_pairs is None:
        discovered_pairs = [
            {
                "pair": pair,
                "base": pair.split("/")[0],
                "quote": pair.split("/")[1],
                "volume_usd": 100000.0,
            }
            for pair in analysis_map
            if pair.endswith("/USD")
        ]

    kraken_pairs = {}
    for pair in {entry["pair"] for entry in discovered_pairs} | set(analysis_map):
        base, quote = pair.split("/")
        kraken_pairs[pair] = {
            "key": pair.replace("/", ""),
            "base": base,
            "quote": quote,
            "pair_decimals": 4,
            "lot_decimals": 6,
            "ordermin": 0.1,
            "costmin": 1.0,
        }

    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        if endpoint == "/api/memory?hours=48&limit=10":
            return {"memories": []}
        if endpoint == "/api/rotation-tree":
            return {"nodes": [], "total_portfolio_value_usd": "35.22"}
        if endpoint.startswith("/api/regime/"):
            return {"trade_gate": 1.0, "regime": "trending", "probabilities": {"volatile": 0.1}}
        if endpoint == "/api/trade-outcomes?lookback_days=7":
            return {"outcomes": []}
        if endpoint == "/api/open-orders":
            return {"orders": []}
        if endpoint == (
            f"/api/memory?category={cc_brain.PERMISSION_BLOCKED_CATEGORY}&hours=999999&limit=1000"
        ):
            return {"memories": permission_memories or []}
        if endpoint == (
            f"/api/memory?category={cc_brain.INSUFFICIENT_QUOTE_CATEGORY}&hours=999999&limit=1000"
        ):
            return {"memories": insufficient_memories or []}
        if endpoint == "/api/memory?category=stuck_dust&hours=48":
            return {"memories": []}
        if endpoint == "/api/exchange-balances":
            return {"balances": exchange_balances, "count": len(exchange_balances)}
        if endpoint.startswith("/api/ohlcv/"):
            return {"bars": [{"close": "1.0"}]}
        if endpoint == "/api/memory?hours=1&limit=100":
            return {"count": len(memory_posts)}
        if endpoint == "/api/memory":
            assert method == "POST"
            memory_posts.append(data)
            return {"id": len(memory_posts), "status": "stored"}
        raise AssertionError(f"unexpected fetch: {method} {endpoint}")

    monkeypatch.setattr(cc_brain, "REVIEWS_DIR", tmp_path / "reviews")
    monkeypatch.setattr(cc_brain, "MAX_POSITION_PCT", 0.5)
    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)
    monkeypatch.setattr(
        cc_brain,
        "compute_portfolio_value",
        lambda: (
            35.22,
            [{"asset": "USD", "qty": 35.22, "price_usd": 1.0, "value_usd": 35.22}],
        ),
    )
    monkeypatch.setattr(cc_brain, "get_asset_volumes", lambda: {})
    monkeypatch.setattr(cc_brain, "discover_all_pairs", lambda limit=40: discovered_pairs)
    monkeypatch.setattr(cc_brain, "_fetch_kraken_pairs", lambda: kraken_pairs)
    monkeypatch.setattr(
        cc_brain,
        "analyze_pair",
        lambda pair: dict(analysis_map[pair]) if pair in analysis_map else None,
    )
    monkeypatch.setattr(
        cc_brain,
        "score_entry",
        lambda analysis: (
            scores[analysis["pair"]],
            {"mock_score": scores[analysis["pair"]]},
        ),
    )
    monkeypatch.setattr(cc_brain, "check_pending_orders", lambda log_fn, dry_run: None)
    monkeypatch.setattr(cc_brain, "immediate_postmortem", lambda losers, log_fn: [])
    monkeypatch.setattr(cc_brain, "deep_postmortem", lambda log_fn: None)
    monkeypatch.setattr(cc_brain, "self_tune", lambda outcomes, analyses, log_fn: None)
    monkeypatch.setattr(cc_brain, "compute_unified_holds", lambda analyses: {})
    monkeypatch.setattr(cc_brain, "evaluate_portfolio", lambda *args, **kwargs: [])
    monkeypatch.setattr(cc_brain, "get_pairs_with_pending_orders", lambda: set())
    monkeypatch.setattr(cc_brain, "get_pairs_with_open_orders", lambda: set())
    monkeypatch.setattr(cc_brain, "check_exits", lambda holdings, analyses, stabilities: [])
    monkeypatch.setattr(cc_brain, "find_dust_positions", lambda *args, **kwargs: [])

    report = cc_brain.run_brain(dry_run=True)
    return report, memory_posts


def test_aleo_usdt_substituted_to_aleo_usd_when_usd_pair_available(monkeypatch, tmp_path):
    report, memory_posts = _run_entry_cycle(
        monkeypatch,
        tmp_path,
        analyses=[
            _analysis("ALEO/USDT", 1.00),
            _analysis("ALEO/USD", 0.99),
        ],
        scores={
            "ALEO/USDT": 1.00,
            "ALEO/USD": 0.95,
        },
        discovered_pairs=[
            {"pair": "ALEO/USD", "base": "ALEO", "quote": "USD", "volume_usd": 100000.0},
        ],
        exchange_balances=[
            {"asset": "USD", "available": "35.22", "held": "0"},
            {"asset": "USDT", "available": "5.17", "held": "0"},
        ],
    )

    decision_posts = [post for post in memory_posts if post["category"] == "decision" and post.get("pair")]
    insufficient_posts = [
        post for post in memory_posts
        if post["category"] == cc_brain.INSUFFICIENT_QUOTE_CATEGORY
    ]

    assert "QUOTE_SUBSTITUTE: ALEO/USDT -> ALEO/USD" in report
    assert "WOULD: buy" in report
    assert "ALEO/USD @" in report
    assert "ALEO/USDT @" not in report
    assert decision_posts[0]["pair"] == "ALEO/USD"
    assert insufficient_posts == []


def test_no_substitution_when_no_usd_alternative(monkeypatch, tmp_path):
    report, memory_posts = _run_entry_cycle(
        monkeypatch,
        tmp_path,
        analyses=[_analysis("ALEO/USDT", 1.00)],
        scores={"ALEO/USDT": 1.00},
        discovered_pairs=[],
        exchange_balances=[
            {"asset": "USD", "available": "35.22", "held": "0"},
            {"asset": "USDT", "available": "5.17", "held": "0"},
        ],
    )

    insufficient_posts = [
        post for post in memory_posts
        if post["category"] == cc_brain.INSUFFICIENT_QUOTE_CATEGORY
    ]
    decision_posts = [post for post in memory_posts if post["category"] == "decision"]

    assert "WOULD:" not in report
    assert len(insufficient_posts) == 1
    assert insufficient_posts[0]["pair"] == "ALEO/USDT"
    assert insufficient_posts[0]["content"]["base"] == "ALEO"
    assert insufficient_posts[0]["content"]["quote"] == "USDT"
    assert insufficient_posts[0]["content"]["available"] == pytest.approx(5.17)
    assert insufficient_posts[0]["content"]["required"] == pytest.approx(17.61)
    assert decision_posts[-1]["content"]["action"] == "hold"


def test_blocked_by_insufficient_quote_inventory_memory(monkeypatch, tmp_path):
    report, memory_posts = _run_entry_cycle(
        monkeypatch,
        tmp_path,
        analyses=[_analysis("ALEO/USDT", 1.00)],
        scores={"ALEO/USDT": 1.00},
        discovered_pairs=[],
        exchange_balances=[
            {"asset": "USD", "available": "35.22", "held": "0"},
            {"asset": "USDT", "available": "5.17", "held": "0"},
        ],
        insufficient_memories=[
            {
                "category": cc_brain.INSUFFICIENT_QUOTE_CATEGORY,
                "pair": "ALEO/USDT",
                "content": {
                    "base": "ALEO",
                    "quote": "USDT",
                    "available": 5.17,
                    "required": 17.61,
                },
            }
        ],
    )

    insufficient_posts = [
        post for post in memory_posts
        if post["category"] == cc_brain.INSUFFICIENT_QUOTE_CATEGORY
    ]
    decision_posts = [post for post in memory_posts if post["category"] == "decision"]

    assert "INSUFFICIENT_QUOTE:" not in report
    assert "WOULD:" not in report
    assert insufficient_posts == []
    assert decision_posts[-1]["content"]["action"] == "hold"
