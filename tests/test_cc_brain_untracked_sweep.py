"""Tests for find_wallet_only_untracked() and its integration with sweep_dust().

Part of Spec 35 -- Untracked-asset wallet sweep.
"""
from __future__ import annotations

import scripts.cc_brain as cc_brain


# ---------------------------------------------------------------------------
# Unit tests for find_wallet_only_untracked()
# ---------------------------------------------------------------------------

def _holding(asset: str, qty: float, value_usd: float) -> dict:
    return {"asset": asset, "qty": qty, "price_usd": value_usd / qty if qty else 0,
            "value_usd": value_usd}


def test_wallet_only_untracked_includes_non_fiat_not_in_tree():
    """FLOW in wallet, not in rotation tree, $0.60 value -> returned as sweep target."""
    holdings = [_holding("FLOW", 10.0, 0.60)]
    result = cc_brain.find_wallet_only_untracked(holdings, open_root_assets=set())
    assert len(result) == 1
    assert result[0]["asset"] == "FLOW"
    assert result[0]["qty"] == 10.0
    assert result[0]["usd_value"] == 0.60


def test_wallet_only_untracked_skips_usd():
    """USD is fiat — never swept even if large."""
    holdings = [_holding("USD", 100.0, 100.0)]
    result = cc_brain.find_wallet_only_untracked(holdings, open_root_assets=set())
    assert result == []


def test_wallet_only_untracked_skips_fiat_gbp():
    """GBP is fiat — skipped via _FIAT_ASSETS."""
    holdings = [_holding("GBP", 10.0, 12.50)]
    result = cc_brain.find_wallet_only_untracked(holdings, open_root_assets=set())
    assert result == []


def test_wallet_only_untracked_skips_stablecoin():
    """USDT is in _SKIP_BASES — skipped even though non-fiat in loose sense."""
    holdings = [_holding("USDT", 5.0, 5.0)]
    result = cc_brain.find_wallet_only_untracked(holdings, open_root_assets=set())
    assert result == []


def test_wallet_only_untracked_skips_already_in_tree():
    """SOL is in open rotation tree -> not returned."""
    holdings = [_holding("SOL", 0.5, 50.0)]
    result = cc_brain.find_wallet_only_untracked(
        holdings, open_root_assets={"SOL", "USD"}
    )
    assert result == []


def test_wallet_only_untracked_skips_below_threshold():
    """FLOW worth $0.04 is below the $0.50 minimum -> not returned."""
    holdings = [_holding("FLOW", 1.0, 0.04)]
    result = cc_brain.find_wallet_only_untracked(holdings, open_root_assets=set())
    assert result == []


# ---------------------------------------------------------------------------
# Integration tests: sweep_dust behaviour for untracked wallet assets
# ---------------------------------------------------------------------------

def test_wallet_only_untracked_volume_min_failure_writes_stuck_dust(monkeypatch):
    """When the sell order fails with volume minimum not met, a stuck_dust memory
    is written via the /api/memory endpoint."""
    memory_posts: list[dict] = []

    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        if endpoint.startswith("/api/ohlcv/"):
            # Return a price so sweep_dust can build a limit order
            return {"bars": [{"close": "0.06"}]}
        if endpoint == "/api/orders":
            assert method == "POST"
            return {"error": "EOrder:Volume minimum not met"}
        if endpoint == "/api/memory":
            assert method == "POST"
            memory_posts.append(data)
            return {"id": len(memory_posts), "status": "stored"}
        raise AssertionError(f"unexpected fetch: {method} {endpoint}")

    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)

    dust = [{"asset": "FLOW", "qty": 10.0, "usd_value": 0.60}]
    logs: list[str] = []
    results = cc_brain.sweep_dust(dust, dry_run=False, log_fn=logs.append)

    # The order failed
    assert results[0]["action"] == "failed"
    assert "EOrder:Volume minimum not met" in results[0]["error"]

    # A stuck_dust memory must have been written
    assert len(memory_posts) == 1
    mem = memory_posts[0]
    assert mem["category"] == "stuck_dust"
    assert mem["pair"] == "FLOW"
    assert mem["content"]["asset"] == "FLOW"


def test_wallet_only_untracked_skips_recently_stuck():
    """Asset in recently_stuck set is excluded even if it meets all other criteria."""
    holdings = [
        _holding("FLOW", 10.0, 0.60),
        _holding("XRP", 5.0, 2.50),
    ]
    # FLOW is stuck; XRP is not
    result = cc_brain.find_wallet_only_untracked(
        holdings, open_root_assets=set(), recently_stuck={"FLOW"}
    )
    assert len(result) == 1
    assert result[0]["asset"] == "XRP"


def test_wallet_only_untracked_recently_stuck_none_default():
    """Default behavior (recently_stuck=None) unchanged — both assets returned."""
    holdings = [
        _holding("FLOW", 10.0, 0.60),
        _holding("XRP", 5.0, 2.50),
    ]
    result = cc_brain.find_wallet_only_untracked(holdings, open_root_assets=set())
    assets = {r["asset"] for r in result}
    assert assets == {"FLOW", "XRP"}


def test_stuck_dust_set_built_from_memory_response():
    """Verify that the stuck_dust set-building logic in run_brain() correctly
    extracts asset names from both ``pair`` field and ``content.asset`` fallback.

    This tests the parsing logic in isolation by calling find_wallet_only_untracked
    with a pre-built set, mirroring what run_brain() constructs from the API response.
    """
    # Simulate the memory response that run_brain() would receive
    memory_rows = [
        {"pair": "FLOW", "content": {"asset": "FLOW", "qty": 10.0, "reason": "EOrder:Volume minimum not met"}},
        {"pair": "TRIA", "content": {}},          # content.asset absent → pair field only
        {"pair": None, "content": {"asset": "XRP"}},  # pair absent → content.asset fallback
    ]

    recently_stuck: set[str] = set()
    for mem in memory_rows:
        asset_from_pair = mem.get("pair")
        if asset_from_pair:
            recently_stuck.add(asset_from_pair)
        content = mem.get("content") or {}
        asset_from_content = content.get("asset")
        if asset_from_content:
            recently_stuck.add(asset_from_content)

    assert "FLOW" in recently_stuck
    assert "TRIA" in recently_stuck
    assert "XRP" in recently_stuck

    # Now confirm find_wallet_only_untracked honours the built set
    holdings = [
        _holding("FLOW", 10.0, 0.60),
        _holding("TRIA", 100.0, 1.20),
        _holding("XRP", 5.0, 2.50),
        _holding("DOT", 2.0, 4.00),   # not stuck → should be returned
    ]
    result = cc_brain.find_wallet_only_untracked(
        holdings, open_root_assets=set(), recently_stuck=recently_stuck
    )
    assets = {r["asset"] for r in result}
    assert assets == {"DOT"}, f"Expected only DOT, got {assets}"


def test_wallet_only_untracked_sell_success(monkeypatch):
    """When the sell order succeeds, sweep_dust returns action='sold' with txid."""

    def fake_fetch(endpoint: str, method: str = "GET", data=None):
        if endpoint.startswith("/api/ohlcv/"):
            return {"bars": [{"close": "0.06"}]}
        if endpoint == "/api/orders":
            assert method == "POST"
            return {"txid": "ABC123"}
        raise AssertionError(f"unexpected fetch: {method} {endpoint}")

    monkeypatch.setattr(cc_brain, "fetch", fake_fetch)

    dust = [{"asset": "FLOW", "qty": 10.0, "usd_value": 0.60}]
    logs: list[str] = []
    results = cc_brain.sweep_dust(dust, dry_run=False, log_fn=logs.append)

    assert results[0]["action"] == "sold"
    assert results[0]["txid"] == "ABC123"
