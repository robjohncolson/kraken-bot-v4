"""Tests for rotation tree planner."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from core.config import load_settings
from core.types import (
    OrderSide,
    RotationCandidate,
    RotationNodeStatus,
    RotationTreeState,
)
from trading.rotation_planner import RotationTreePlanner


NOW = datetime(2026, 3, 31, 14, 0, tzinfo=timezone.utc)


def _settings(**overrides):
    defaults = {
        "KRAKEN_API_KEY": "test",
        "KRAKEN_API_SECRET": "test",
        "MIN_POSITION_USD": "5",
        "MAX_POSITION_USD": "100",
    }
    defaults.update(overrides)
    return load_settings(defaults)


def _mock_scanner(candidates: tuple[RotationCandidate, ...] = ()) -> MagicMock:
    scanner = MagicMock()
    scanner.scan_rotation_candidates.return_value = candidates
    return scanner


def _candidate(
    *,
    pair: str = "ETH/USD",
    from_asset: str = "USD",
    to_asset: str = "ETH",
    confidence: float = 0.75,
) -> RotationCandidate:
    return RotationCandidate(
        pair=pair,
        from_asset=from_asset,
        to_asset=to_asset,
        order_side=OrderSide.BUY,
        confidence=confidence,
        reference_price_hint=Decimal("3000"),
        estimated_window_hours=12.0,
    )


def test_initialize_roots() -> None:
    settings = _settings()
    scanner = _mock_scanner()
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("80"), "DOGE": Decimal("5000")},
        prices_usd={"USD": Decimal("1"), "DOGE": Decimal("0.09")},
    )
    assert len(tree.nodes) == 2
    assert len(tree.root_node_ids) == 2


def test_plan_cycle_creates_children() -> None:
    settings = _settings()
    candidates = (
        RotationCandidate(
            pair="ETH/USD", from_asset="USD", to_asset="ETH",
            order_side=OrderSide.BUY, confidence=0.75,
            reference_price_hint=Decimal("3000"),
            estimated_window_hours=12.0,
        ),
    )
    scanner = _mock_scanner(candidates)
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("100")},
        prices_usd={"USD": Decimal("1")},
    )
    result = planner.plan_cycle(tree, NOW)

    # Should have root + 1 planned child
    assert len(result.nodes) == 2
    child = [n for n in result.nodes if n.depth == 1]
    assert len(child) == 1
    assert child[0].asset == "ETH"
    assert child[0].status == RotationNodeStatus.PLANNED


def test_plan_cycle_respects_depth_limit() -> None:
    settings = _settings()
    candidates = (
        RotationCandidate(
            pair="SOL/ETH", from_asset="ETH", to_asset="SOL",
            order_side=OrderSide.BUY, confidence=0.8,
            reference_price_hint=Decimal("100"),
            estimated_window_hours=6.0,
        ),
    )
    scanner = _mock_scanner(candidates)
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    # Build a depth-2 tree manually (root → child at max depth)
    from core.types import RotationNode
    root = RotationNode(
        node_id="root-usd", parent_node_id=None, depth=0,
        asset="USD", quantity_total=Decimal("100"), quantity_free=Decimal("20"),
        status=RotationNodeStatus.OPEN,
    )
    child = RotationNode(
        node_id="root-usd-eth-0", parent_node_id="root-usd", depth=1,
        asset="ETH", quantity_total=Decimal("50"), quantity_free=Decimal("50"),
        status=RotationNodeStatus.OPEN,
    )
    grandchild = RotationNode(
        node_id="root-usd-eth-0-sol-0", parent_node_id="root-usd-eth-0", depth=2,
        asset="SOL", quantity_total=Decimal("20"), quantity_free=Decimal("20"),
        status=RotationNodeStatus.OPEN,
    )
    tree = RotationTreeState(
        nodes=(root, child, grandchild),
        root_node_ids=("root-usd",),
        max_depth=2,
    )
    result = planner.plan_cycle(tree, NOW)

    # Grandchild at depth 2 should NOT spawn children (depth limit)
    assert len(result.nodes) == 3  # No new nodes


def test_plan_cycle_skips_low_confidence() -> None:
    settings = _settings()
    candidates = (
        RotationCandidate(
            pair="ETH/USD", from_asset="USD", to_asset="ETH",
            order_side=OrderSide.BUY, confidence=0.4,  # Below MIN_CONFIDENCE
            reference_price_hint=Decimal("3000"),
            estimated_window_hours=12.0,
        ),
    )
    scanner = _mock_scanner(candidates)
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("100")}, prices_usd={"USD": Decimal("1")},
    )
    result = planner.plan_cycle(tree, NOW)
    assert len(result.nodes) == 1  # Root only, no children


def test_plan_cycle_no_double_plan() -> None:
    settings = _settings()
    scanner = _mock_scanner()
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("100")}, prices_usd={"USD": Decimal("1")},
    )
    result1 = planner.plan_cycle(tree, NOW)
    result2 = planner.plan_cycle(result1, NOW)  # Same time — should skip

    assert result2.last_planned_at == result1.last_planned_at


def test_plan_cycle_dynamic_max_children_small_budget() -> None:
    settings = _settings(
        MIN_POSITION_USD="10",
        ROTATION_MAX_CHILDREN_PER_PARENT="3",
    )
    scanner = _mock_scanner(
        (
            _candidate(pair="ETH/USD", to_asset="ETH", confidence=0.9),
            _candidate(pair="SOL/USD", to_asset="SOL", confidence=0.86),
            _candidate(pair="ADA/USD", to_asset="ADA", confidence=0.82),
        )
    )
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("15")},
        prices_usd={"USD": Decimal("1")},
    )
    result = planner.plan_cycle(tree, NOW)

    children = [node for node in result.nodes if node.depth == 1]
    assert len(children) == 1


def test_plan_cycle_dynamic_max_children_large_budget() -> None:
    settings = _settings(
        MIN_POSITION_USD="10",
        ROTATION_MAX_CHILDREN_PER_PARENT="3",
    )
    scanner = _mock_scanner(
        (
            _candidate(pair="ETH/USD", to_asset="ETH", confidence=0.9),
            _candidate(pair="SOL/USD", to_asset="SOL", confidence=0.86),
            _candidate(pair="ADA/USD", to_asset="ADA", confidence=0.84),
            _candidate(pair="AVAX/USD", to_asset="AVAX", confidence=0.82),
        )
    )
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("100")},
        prices_usd={"USD": Decimal("1")},
    )
    result = planner.plan_cycle(tree, NOW)

    children = [node for node in result.nodes if node.depth == 1]
    assert len(children) == 3


def test_plan_cycle_uses_rotation_min_confidence() -> None:
    settings = _settings(ROTATION_MIN_CONFIDENCE="0.65")
    scanner = _mock_scanner((_candidate(confidence=0.67),))
    planner = RotationTreePlanner(settings=settings, pair_scanner=scanner)

    tree = planner.initialize_roots(
        {"USD": Decimal("100")},
        prices_usd={"USD": Decimal("1")},
    )
    result = planner.plan_cycle(tree, NOW)

    children = [node for node in result.nodes if node.depth == 1]
    assert len(children) == 1


def test_kelly_fraction_below_sample_gate() -> None:
    settings = _settings(KELLY_MIN_SAMPLE_SIZE="10")
    scanner = _mock_scanner()
    db_writer = MagicMock()
    db_writer.fetch_child_trade_stats.return_value = (3, 2, Decimal("1.5"))
    planner = RotationTreePlanner(
        settings=settings,
        pair_scanner=scanner,
        db_writer=db_writer,
    )

    assert planner._kelly_fraction() is None
