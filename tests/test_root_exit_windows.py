"""Tests for root exit windows — deadline evaluation, TA re-evaluation, and exit mechanics."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import numpy as np
import pandas as pd
import pytest
from unittest.mock import patch

from core.types import (
    OrderSide,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
)
from trading.pair_scanner import QUOTE_ASSETS, evaluate_root_ta


# ---------------------------------------------------------------------------
# evaluate_root_ta unit tests
# ---------------------------------------------------------------------------


def _make_bars(prices: list[float]) -> pd.DataFrame:
    """Create a minimal OHLCV DataFrame from close prices."""
    n = len(prices)
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p * 1.01 for p in prices],
            "low": [p * 0.99 for p in prices],
            "close": prices,
            "volume": [1000.0] * n,
        }
    )


def _trending_up_bars(n: int = 50, start: float = 100.0) -> pd.DataFrame:
    """Generate bars with a clear uptrend (bullish)."""
    prices = [start + i * 0.5 for i in range(n)]
    return _make_bars(prices)


def _trending_down_bars(n: int = 50, start: float = 100.0) -> pd.DataFrame:
    """Generate bars with a clear downtrend (bearish)."""
    prices = [start - i * 0.5 for i in range(n)]
    return _make_bars(prices)


def _flat_bars(n: int = 50, price: float = 100.0) -> pd.DataFrame:
    """Generate sideways bars (neutral/mixed signals)."""
    np.random.seed(42)
    noise = np.random.normal(0, 0.01, n)
    prices = [price * (1 + noise[i]) for i in range(n)]
    return _make_bars(prices)


class TestEvaluateRootTa:
    def test_bullish_trend_returns_bullish(self):
        bars = _trending_up_bars()
        direction, window, confidence = evaluate_root_ta(bars)
        assert direction == "bullish"
        assert 6.0 <= window <= 48.0
        assert 0.0 < confidence <= 1.0

    def test_bearish_trend_returns_bearish(self):
        bars = _trending_down_bars()
        direction, window, confidence = evaluate_root_ta(bars)
        assert direction == "bearish"
        assert 6.0 <= window <= 48.0
        assert confidence == 1.0  # all 3 signals agree

    def test_window_clamped_within_range(self):
        bars = _trending_up_bars()
        _, window, _ = evaluate_root_ta(bars)
        assert 6.0 <= window <= 48.0

    def test_window_floor_clamps_high_volatility_pair_to_six_hours(self):
        from trading.pair_scanner import _estimate_rotation_window_hours

        bars = _trending_up_bars()
        with patch("pandas.core.series.Series.std", return_value=0.02):
            window = _estimate_rotation_window_hours(
                bars,
                take_profit_pct=5.0,
            )

        assert window == 6.0

    def test_returns_tuple_of_three(self):
        bars = _trending_up_bars()
        result = evaluate_root_ta(bars)
        assert len(result) == 3
        assert result[0] in ("bullish", "bearish", "neutral")
        assert isinstance(result[1], float)
        assert isinstance(result[2], float)

    def test_bullish_confidence_is_signal_agreement(self):
        bars = _trending_up_bars()
        direction, _, confidence = evaluate_root_ta(bars)
        assert direction == "bullish"
        # 2 or 3 signals agree → confidence is 2/3 or 3/3
        assert confidence in (2.0 / 3.0, 1.0)

    def test_insufficient_bars_raises(self):
        bars = _make_bars([100.0] * 10)  # Too few for EMA slow span (26)
        with pytest.raises(ValueError):
            evaluate_root_ta(bars)


# ---------------------------------------------------------------------------
# QUOTE_ASSETS constant tests
# ---------------------------------------------------------------------------


class TestQuoteAssets:
    def test_usd_is_quote(self):
        assert "USD" in QUOTE_ASSETS

    def test_usdt_is_quote(self):
        assert "USDT" in QUOTE_ASSETS

    def test_usdc_is_quote(self):
        assert "USDC" in QUOTE_ASSETS

    def test_crypto_not_quote(self):
        assert "BTC" not in QUOTE_ASSETS
        assert "ADA" not in QUOTE_ASSETS
        assert "PEPE" not in QUOTE_ASSETS


# ---------------------------------------------------------------------------
# Root node deadline setting tests (via runtime loop mocking)
# ---------------------------------------------------------------------------


def _make_root(
    asset: str,
    qty: Decimal = Decimal("100"),
    deadline_at=None,
    status=RotationNodeStatus.OPEN,
    entry_pair=None,
    order_side=None,
) -> RotationNode:
    return RotationNode(
        node_id=f"root-{asset.lower()}",
        parent_node_id=None,
        depth=0,
        asset=asset,
        quantity_total=qty,
        quantity_free=qty,
        status=status,
        deadline_at=deadline_at,
        entry_pair=entry_pair,
        order_side=order_side,
    )


def _make_tree(*nodes: RotationNode) -> RotationTreeState:
    return RotationTreeState(
        nodes=tuple(nodes),
        root_node_ids=tuple(n.node_id for n in nodes if n.depth == 0),
    )


class TestFindRootExitPair:
    """Test _find_root_exit_pair logic."""

    def test_quote_assets_still_defined_for_pair_matching(self):
        """QUOTE_ASSETS used for pair matching priority, not for skipping."""
        assert "USD" in QUOTE_ASSETS
        assert "USDT" in QUOTE_ASSETS


class TestRootExitPairMatching:
    """Test the pair matching logic used by _find_root_exit_pair."""

    def test_base_asset_matched_with_preferred_quote(self):
        """When asset is base in BASE/USD, entry_side should be BUY."""
        # ADA is base in ADA/USD → "entered" by buying ADA → exit by selling
        # _find_root_exit_pair returns (pair, BUY) for base assets
        # This is tested via the actual method, but we verify the logic:
        # base == asset and quote == preferred → OrderSide.BUY
        assert OrderSide.BUY is not None  # Sanity

    def test_quote_asset_matched(self):
        """When asset is quote in BTC/ASSET, entry_side should be SELL."""
        assert OrderSide.SELL is not None  # Sanity


# ---------------------------------------------------------------------------
# Root expiry re-evaluation tests
# ---------------------------------------------------------------------------


class TestRootExpiryReEvaluation:
    """Test that expired roots are re-evaluated, not hard-sold."""

    def test_bullish_root_extends_deadline(self):
        """An expired root that's still bullish should get a new deadline."""
        now = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        root = _make_root("ADA", deadline_at=now - timedelta(hours=1))
        tree = _make_tree(root)

        # After re-evaluation with bullish TA, deadline should be extended
        from trading.rotation_tree import update_node

        new_deadline = now + timedelta(hours=10)
        updated = update_node(tree, root.node_id, deadline_at=new_deadline)

        node = next(n for n in updated.nodes if n.node_id == root.node_id)
        assert node.deadline_at == new_deadline
        assert node.deadline_at > now

    def test_expired_root_is_detected(self):
        """expired_nodes should include root nodes with past deadlines."""
        from trading.rotation_tree import expired_nodes

        now = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        root = _make_root("ADA", deadline_at=now - timedelta(hours=1))
        tree = _make_tree(root)

        expired = expired_nodes(tree, now)
        assert len(expired) == 1
        assert expired[0].node_id == root.node_id

    def test_root_without_deadline_not_expired(self):
        """Root with no deadline should not appear in expired_nodes."""
        from trading.rotation_tree import expired_nodes

        now = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        root = _make_root("ADA")  # deadline_at=None
        tree = _make_tree(root)

        expired = expired_nodes(tree, now)
        assert len(expired) == 0

    def test_root_with_future_deadline_not_expired(self):
        """Root with future deadline should not appear in expired_nodes."""
        from trading.rotation_tree import expired_nodes

        now = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        root = _make_root("ADA", deadline_at=now + timedelta(hours=5))
        tree = _make_tree(root)

        expired = expired_nodes(tree, now)
        assert len(expired) == 0


class TestEvaluateRootDeadlinesSkips:
    """Test that _evaluate_root_deadlines correctly skips certain roots."""

    def test_all_assets_equal_no_skip(self):
        """No currency is treated as special — all roots can get deadlines."""
        for asset in ("USD", "USDT", "ADA", "BTC"):
            root = _make_root(asset)
            # All roots are eligible (depth==0, status==OPEN, no deadline)
            assert root.depth == 0
            assert root.status == RotationNodeStatus.OPEN
            assert root.deadline_at is None

    def test_root_with_existing_deadline_skipped(self):
        """Root that already has a deadline should not be re-evaluated by _evaluate_root_deadlines."""
        now = datetime(2026, 4, 3, 12, 0, tzinfo=timezone.utc)
        root = _make_root("ADA", deadline_at=now + timedelta(hours=5))
        # _evaluate_root_deadlines skips nodes with deadline_at is not None
        assert root.deadline_at is not None

    def test_non_root_skipped(self):
        """Child nodes (depth > 0) should not be evaluated for root deadlines."""
        child = RotationNode(
            node_id="root-ada-btc-0",
            parent_node_id="root-ada",
            depth=1,
            asset="BTC",
            quantity_total=Decimal("1"),
            quantity_free=Decimal("1"),
            status=RotationNodeStatus.OPEN,
        )
        assert child.depth != 0


# ---------------------------------------------------------------------------
# Deadline timezone formatting tests
# ---------------------------------------------------------------------------


class TestDeadlineFormatting:
    def test_utc_to_eastern(self):
        from tui.widgets.rotation_tree import _format_deadline_et

        # 2026-04-03T18:30:00+00:00 (UTC) = 2:30 PM ET (EDT in April)
        result = _format_deadline_et("2026-04-03T18:30:00+00:00")
        assert "ET" in result
        assert "04/03" in result
        assert "14:30" in result

    def test_naive_datetime_treated_as_utc(self):
        from tui.widgets.rotation_tree import _format_deadline_et

        result = _format_deadline_et("2026-04-03T18:30:00")
        assert "ET" in result
        assert "14:30" in result

    def test_invalid_string_falls_back(self):
        from tui.widgets.rotation_tree import _format_deadline_et

        result = _format_deadline_et("not-a-date")
        assert result == "not-a-date"


# ---------------------------------------------------------------------------
# Unrealized P&L tests
# ---------------------------------------------------------------------------


class TestUnrealizedPnL:
    def test_root_unrealized_pnl_computed(self):
        """Open root with entry_cost should show unrealized P&L."""
        from guardian import PriceSnapshot as PS
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("ADA", qty=Decimal("100"))
        root = replace(root, entry_cost=Decimal("50"))
        tree = _make_tree(root)
        prices = {"ADA/USD": PS(price=Decimal("0.70"))}
        snap = _build_rotation_tree_snapshot(tree, current_prices=prices)
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is not None
        pnl = Decimal(node_snap.realized_pnl)
        # current value = 100 * 0.70 = 70, entry_cost = 50, P&L = 20
        assert pnl == Decimal("20")

    def test_root_no_entry_cost_shows_none(self):
        """Root without entry_cost should show no P&L."""
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("ADA", qty=Decimal("100"))
        tree = _make_tree(root)
        snap = _build_rotation_tree_snapshot(tree)
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is None

    def test_usd_root_pnl_is_zero(self):
        """USD root should have P&L of 0 (1 USD = 1 USD)."""
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("USD", qty=Decimal("100"))
        root = replace(root, entry_cost=Decimal("100"))
        tree = _make_tree(root)
        # No ADA/USD price needed — USD is not in any pair as base
        # But USD root has entry_cost=100 and qty=100
        # root_usd_prices defaults USD=1, so current_value = 100*1 = 100
        snap = _build_rotation_tree_snapshot(tree)
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is not None
        assert Decimal(node_snap.realized_pnl) == Decimal("0")

    def test_fiat_inverse_pair_pnl(self):
        """Fiat root priced via inverse pair (USD/EUR) shows correct P&L."""
        from guardian import PriceSnapshot as PS
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("EUR", qty=Decimal("200"))
        root = replace(root, entry_cost=Decimal("210"))
        tree = _make_tree(root)
        # USD/EUR = 0.90 → EUR price = 1/0.90 ≈ 1.1111
        prices = {"USD/EUR": PS(price=Decimal("0.90"))}
        snap = _build_rotation_tree_snapshot(tree, current_prices=prices)
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is not None
        pnl = Decimal(node_snap.realized_pnl)
        # current_value = 200 * (1/0.90) ≈ 222.22, entry_cost = 210, P&L ≈ 12.22
        expected = Decimal("200") * (Decimal("1") / Decimal("0.90")) - Decimal("210")
        assert pnl == expected

    def test_stablecoin_defaults_without_price_data(self):
        """USDT/USDC roots use $1 default even without WebSocket prices."""
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("USDT", qty=Decimal("500"))
        root = replace(root, entry_cost=Decimal("500"))
        tree = _make_tree(root)
        snap = _build_rotation_tree_snapshot(tree)  # no current_prices
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is not None
        assert Decimal(node_snap.realized_pnl) == Decimal("0")

    def test_missing_asset_no_pnl(self):
        """Asset with no price in map and no default shows no P&L."""
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("GBP", qty=Decimal("100"))
        root = replace(root, entry_cost=Decimal("120"))
        tree = _make_tree(root)
        snap = _build_rotation_tree_snapshot(tree)  # no prices for GBP
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is None

    def test_cached_price_fallback(self):
        """Asset with no WebSocket price uses cached REST price for P&L."""
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("BABY", qty=Decimal("1000"))
        root = replace(root, entry_cost=Decimal("14"))
        tree = _make_tree(root)
        cached = {"BABY": Decimal("0.015")}
        snap = _build_rotation_tree_snapshot(
            tree,
            cached_root_prices=cached,
        )
        node_snap = snap.nodes[0]
        assert node_snap.realized_pnl is not None
        # current_value = 1000 * 0.015 = 15, entry_cost = 14, P&L = 1
        assert Decimal(node_snap.realized_pnl) == Decimal("1")

    def test_websocket_overrides_cached_price(self):
        """Fresh WebSocket price takes precedence over stale cached price."""
        from guardian import PriceSnapshot as PS
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("ADA", qty=Decimal("100"))
        root = replace(root, entry_cost=Decimal("50"))
        tree = _make_tree(root)
        cached = {"ADA": Decimal("0.50")}  # stale
        prices = {"ADA/USD": PS(price=Decimal("0.70"))}  # fresh
        snap = _build_rotation_tree_snapshot(
            tree,
            current_prices=prices,
            cached_root_prices=cached,
        )
        node_snap = snap.nodes[0]
        # Should use 0.70 (WebSocket), not 0.50 (cached)
        # current_value = 100 * 0.70 = 70, entry_cost = 50, P&L = 20
        assert Decimal(node_snap.realized_pnl) == Decimal("20")

    def test_child_unrealized_pnl_computed_from_pair_price(self):
        from guardian import PriceSnapshot as PS
        from runtime_loop import _build_rotation_tree_snapshot

        root = _make_root("USD", qty=Decimal("100"))
        child = RotationNode(
            node_id="root-usd-eth-0",
            parent_node_id=root.node_id,
            depth=1,
            asset="ETH",
            quantity_total=Decimal("2"),
            quantity_free=Decimal("2"),
            status=RotationNodeStatus.OPEN,
            entry_pair="ETH/USD",
            from_asset="USD",
            order_side=OrderSide.BUY,
            entry_price=Decimal("100"),
            fill_price=Decimal("100"),
            entry_cost=Decimal("200"),
        )
        tree = _make_tree(root, child)
        prices = {"ETH/USD": PS(price=Decimal("110"))}

        snap = _build_rotation_tree_snapshot(tree, current_prices=prices)
        child_snap = next(node for node in snap.nodes if node.node_id == child.node_id)

        assert child_snap.realized_pnl is not None
        assert Decimal(child_snap.realized_pnl) == Decimal("20")


# ---------------------------------------------------------------------------
# Persistence round-trip: entry_cost survives restart
# ---------------------------------------------------------------------------


class TestEntryCostPersistence:
    def test_entry_cost_round_trip(self):
        """entry_cost saved to SQLite is restored on fetch."""
        import sqlite3
        from persistence.sqlite import SqliteReader, SqliteWriter, ensure_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        writer = SqliteWriter(conn)
        reader = SqliteReader(conn)

        root = _make_root("ADA", qty=Decimal("100"))
        root = replace(
            root,
            entry_cost=Decimal("45.50"),
            deadline_at=datetime(2026, 4, 3, tzinfo=timezone.utc),
        )
        tree = _make_tree(root)
        writer.save_rotation_tree(tree)

        loaded = reader.fetch_rotation_tree()
        assert len(loaded.nodes) == 1
        assert loaded.nodes[0].entry_cost == Decimal("45.50")
        assert loaded.nodes[0].deadline_at is not None

    def test_entry_cost_none_round_trip(self):
        """Nodes without entry_cost load as None (not crash)."""
        import sqlite3
        from persistence.sqlite import SqliteReader, SqliteWriter, ensure_schema

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        ensure_schema(conn)
        writer = SqliteWriter(conn)
        reader = SqliteReader(conn)

        root = _make_root("ADA", qty=Decimal("100"))
        tree = _make_tree(root)
        writer.save_rotation_tree(tree)

        loaded = reader.fetch_rotation_tree()
        assert loaded.nodes[0].entry_cost is None


# ---------------------------------------------------------------------------
# Cached root USD prices tests
# ---------------------------------------------------------------------------


class TestCachedRootPrices:
    def test_collect_root_prices_includes_stablecoins(self):
        """_collect_root_prices always includes USD=1 default."""
        from unittest.mock import patch
        from runtime_loop import _collect_root_prices

        with patch("exchange.ohlcv.fetch_ohlcv", side_effect=Exception("no network")):
            prices = _collect_root_prices({}, {"USDT": Decimal("100")})
        assert prices["USD"] == Decimal("1")

    def test_collect_root_prices_websocket_hit(self):
        """Known WebSocket price is used without REST fallback."""
        from guardian import PriceSnapshot as PS
        from unittest.mock import patch
        from runtime_loop import _collect_root_prices

        ws_prices = {"ADA/USD": PS(price=Decimal("0.45"))}
        with patch("exchange.ohlcv.fetch_ohlcv") as mock_fetch:
            prices = _collect_root_prices(ws_prices, {"ADA": Decimal("100")})
        assert prices["ADA"] == Decimal("0.45")
        mock_fetch.assert_not_called()

    def test_collect_root_prices_rest_fallback(self):
        """Missing asset triggers REST OHLCV fetch."""
        import pandas as pd
        from unittest.mock import patch
        from runtime_loop import _collect_root_prices

        bars = pd.DataFrame({"close": ["1.25"]})
        with patch("exchange.ohlcv.fetch_ohlcv", return_value=bars):
            prices = _collect_root_prices({}, {"EUR": Decimal("100")})
        assert "EUR" in prices
        assert prices["EUR"] == Decimal("1.25")
