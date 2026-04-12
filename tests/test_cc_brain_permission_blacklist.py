from __future__ import annotations

from datetime import datetime

import scripts.cc_brain as cc_brain


def test_permission_blocked_pair_filtered(monkeypatch):
    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        assert method == "GET"
        assert data is None
        assert endpoint == "/api/memory?category=permission_blocked&hours=999999&limit=1000"
        return {
            "memories": [
                {
                    "category": "permission_blocked",
                    "pair": "AUD/USD",
                    "content": {
                        "pair": "AUD/USD",
                        "error_text": "EAccount:Invalid permissions:AUD/USD trading restricted for US:MA",
                        "first_blocked_ts": "2026-04-12T12:00:00+00:00",
                    },
                }
            ]
        }

    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)

    blocked_pairs = cc_brain.load_permission_blocked()
    logs: list[str] = []
    filtered = cc_brain.filter_permission_blocked_orders(
        [
            {"pair": "AUD/USD", "side": "sell"},
            {"pair": "BTC/USD", "side": "buy"},
        ],
        blocked_pairs,
        logs.append,
    )

    assert blocked_pairs == {"AUD/USD"}
    assert [order["pair"] for order in filtered] == ["BTC/USD"]
    assert logs == ["  Filtered 1 permission-blocked order(s): ['AUD/USD']"]


def test_permission_failure_persists_memory(monkeypatch):
    memory_posts: list[dict] = []

    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        assert endpoint == "/api/memory"
        assert method == "POST"
        memory_posts.append(data)
        return {"id": 1, "status": "stored"}

    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)

    logs: list[str] = []
    cc_brain.persist_permission_blocked_memory(
        {"pair": "AUD/USD", "side": "sell"},
        "Exchange error: EAccount:Invalid permissions:AUD/USD trading restricted for US:MA",
        logs.append,
    )

    assert len(memory_posts) == 1
    payload = memory_posts[0]
    assert payload["category"] == "permission_blocked"
    assert payload["pair"] == "AUD/USD"
    assert payload["importance"] == 0.9
    assert payload["content"]["pair"] == "AUD/USD"
    assert payload["content"]["error_text"] == (
        "Exchange error: EAccount:Invalid permissions:AUD/USD trading restricted for US:MA"
    )
    datetime.fromisoformat(payload["content"]["first_blocked_ts"])
    assert logs == ["  -> persisted permission_blocked for AUD/USD"]
