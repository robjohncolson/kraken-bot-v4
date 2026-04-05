"""Tests for rotation tree helpers."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from core.types import (
    OrderSide,
    RotationCandidate,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
)
from trading.rotation_tree import (
    add_node,
    build_root_nodes,
    cascade_close,
    children_of,
    close_node,
    compute_child_allocations,
    expired_nodes,
    leaf_nodes,
    make_child_node,
    node_by_id,
    remaining_hours,
    update_node,
)

NOW = datetime(2026, 3, 31, 14, 0, tzinfo=timezone.utc)


def _root(asset: str = "USD", qty: Decimal = Decimal("100")) -> RotationNode:
    return RotationNode(
        node_id=f"root-{asset.lower()}",
        parent_node_id=None,
        depth=0,
        asset=asset,
        quantity_total=qty,
        quantity_free=qty,
        status=RotationNodeStatus.OPEN,
    )


def _candidate(
    pair: str = "ETH/USD",
    from_asset: str = "USD",
    to_asset: str = "ETH",
    confidence: float = 0.7,
    window: float = 12.0,
) -> RotationCandidate:
    return RotationCandidate(
        pair=pair,
        from_asset=from_asset,
        to_asset=to_asset,
        order_side=OrderSide.BUY,
        confidence=confidence,
        reference_price_hint=Decimal("100"),
        estimated_window_hours=window,
    )


def test_build_root_nodes_from_balances() -> None:
    balances = {
        "USD": Decimal("80"),
        "DOGE": Decimal("5000"),
        "TRIA": Decimal("0.0001"),
    }
    prices = {"USD": Decimal("1"), "DOGE": Decimal("0.09"), "TRIA": Decimal("0.001")}
    roots = build_root_nodes(balances, min_value_usd=Decimal("1"), prices_usd=prices)
    assets = {r.asset for r in roots}
    assert "USD" in assets
    assert "DOGE" in assets
    assert "TRIA" not in assets  # dust


def test_children_of() -> None:
    parent = _root()
    child = RotationNode(
        node_id="root-usd-eth-0",
        parent_node_id="root-usd",
        depth=1,
        asset="ETH",
        quantity_total=Decimal("50"),
        quantity_free=Decimal("50"),
        status=RotationNodeStatus.OPEN,
    )
    tree = RotationTreeState(nodes=(parent, child))
    assert len(children_of(tree, "root-usd")) == 1
    assert children_of(tree, "root-usd")[0].node_id == "root-usd-eth-0"


def test_leaf_nodes() -> None:
    root = _root()
    child = RotationNode(
        node_id="root-usd-eth-0",
        parent_node_id="root-usd",
        depth=1,
        asset="ETH",
        quantity_total=Decimal("50"),
        quantity_free=Decimal("50"),
        status=RotationNodeStatus.OPEN,
    )
    tree = RotationTreeState(nodes=(root, child))
    leaves = leaf_nodes(tree)
    assert len(leaves) == 1
    assert leaves[0].node_id == "root-usd-eth-0"


def test_remaining_hours() -> None:
    node = replace(_root(), deadline_at=NOW + timedelta(hours=6))
    assert remaining_hours(node, NOW) == 6.0
    assert remaining_hours(_root(), NOW) is None


def test_compute_allocations_single_candidate() -> None:
    parent = _root(qty=Decimal("100"))
    candidates = (_candidate(confidence=0.83),)
    allocs = compute_child_allocations(parent, candidates)
    assert len(allocs) == 1
    _, qty = allocs[0]
    assert qty > 0
    assert qty <= Decimal("60")  # MAX_CHILD_RATIO


def test_compute_allocations_below_min_confidence() -> None:
    parent = _root(qty=Decimal("100"))
    candidates = (_candidate(confidence=0.4),)
    allocs = compute_child_allocations(parent, candidates)
    assert len(allocs) == 0


def test_compute_allocations_two_candidates() -> None:
    parent = _root(qty=Decimal("200"))
    candidates = (
        _candidate(pair="ETH/USD", to_asset="ETH", confidence=0.9),
        _candidate(pair="SOL/USD", to_asset="SOL", confidence=0.83),
    )
    allocs = compute_child_allocations(parent, candidates, min_position=Decimal("5"))
    assert len(allocs) == 2
    total = sum(qty for _, qty in allocs)
    assert total <= Decimal("160")  # PARENT_DEPLOY_RATIO * 200


def test_compute_allocations_rejects_four_of_six_confidence() -> None:
    parent = _root(qty=Decimal("100"))
    candidates = (_candidate(confidence=0.67),)
    allocs = compute_child_allocations(parent, candidates)
    assert allocs == []


def test_compute_allocations_accepts_five_of_six_confidence() -> None:
    parent = _root(qty=Decimal("100"))
    candidates = (_candidate(confidence=0.83),)
    allocs = compute_child_allocations(parent, candidates)
    assert len(allocs) == 1


def test_make_child_node_respects_parent_deadline() -> None:
    parent = replace(_root(), deadline_at=NOW + timedelta(hours=4))
    candidate = _candidate(window=12.0)
    child = make_child_node(parent, candidate, Decimal("50"), NOW)
    assert child.depth == 1
    assert child.deadline_at == NOW + timedelta(hours=4)  # Capped by parent


def test_make_child_node_no_parent_deadline() -> None:
    parent = _root()
    candidate = _candidate(window=8.0)
    child = make_child_node(parent, candidate, Decimal("50"), NOW)
    assert child.deadline_at == NOW + timedelta(hours=8)


def test_close_node() -> None:
    tree = RotationTreeState(nodes=(_root(),))
    closed = close_node(tree, "root-usd")
    assert closed.nodes[0].status == RotationNodeStatus.CLOSED


def test_cascade_close() -> None:
    root = _root()
    child = RotationNode(
        node_id="root-usd-eth-0",
        parent_node_id="root-usd",
        depth=1,
        asset="ETH",
        quantity_total=Decimal("50"),
        quantity_free=Decimal("50"),
        status=RotationNodeStatus.OPEN,
    )
    grandchild = RotationNode(
        node_id="root-usd-eth-0-sol-0",
        parent_node_id="root-usd-eth-0",
        depth=2,
        asset="SOL",
        quantity_total=Decimal("20"),
        quantity_free=Decimal("20"),
        status=RotationNodeStatus.OPEN,
    )
    tree = RotationTreeState(nodes=(root, child, grandchild))
    result = cascade_close(tree, "root-usd")
    for n in result.nodes:
        assert n.status == RotationNodeStatus.EXPIRED


def test_expired_nodes() -> None:
    past = NOW - timedelta(hours=1)
    future = NOW + timedelta(hours=1)
    expired_node = replace(_root(), node_id="expired", deadline_at=past)
    live_node = RotationNode(
        node_id="live",
        parent_node_id=None,
        depth=0,
        asset="ETH",
        quantity_total=Decimal("10"),
        quantity_free=Decimal("10"),
        deadline_at=future,
        status=RotationNodeStatus.OPEN,
    )
    tree = RotationTreeState(nodes=(expired_node, live_node))
    result = expired_nodes(tree, NOW)
    assert len(result) == 1
    assert result[0].node_id == "expired"


def test_add_and_update_node() -> None:
    tree = RotationTreeState()
    tree = add_node(tree, _root())
    assert len(tree.nodes) == 1
    tree = update_node(tree, "root-usd", quantity_free=Decimal("50"))
    assert tree.nodes[0].quantity_free == Decimal("50")


# ---------------------------------------------------------------------------
# cancel_planned_node tests
# ---------------------------------------------------------------------------


def test_cancel_planned_node_returns_reserved_qty_to_parent() -> None:
    from trading.rotation_tree import cancel_planned_node

    parent = RotationNode(
        node_id="root-usd",
        parent_node_id=None,
        depth=0,
        asset="USD",
        quantity_total=Decimal("100"),
        quantity_free=Decimal("40"),
        quantity_reserved=Decimal("60"),
        status=RotationNodeStatus.OPEN,
    )
    child = RotationNode(
        node_id="child-eth",
        parent_node_id="root-usd",
        depth=1,
        asset="ETH",
        quantity_total=Decimal("60"),
        quantity_free=Decimal("60"),
        status=RotationNodeStatus.PLANNED,
    )
    tree = RotationTreeState(nodes=(parent, child))

    tree = cancel_planned_node(tree, "child-eth")

    updated_parent = node_by_id(tree, "root-usd")
    updated_child = node_by_id(tree, "child-eth")

    assert updated_parent.quantity_free == Decimal("100")
    assert updated_parent.quantity_reserved == Decimal("0")
    assert updated_child.status == RotationNodeStatus.CANCELLED


def test_cancel_planned_node_is_noop_for_non_planned_node() -> None:
    from trading.rotation_tree import cancel_planned_node

    parent = RotationNode(
        node_id="root-usd",
        parent_node_id=None,
        depth=0,
        asset="USD",
        quantity_total=Decimal("100"),
        quantity_free=Decimal("40"),
        quantity_reserved=Decimal("60"),
        status=RotationNodeStatus.OPEN,
    )
    child = RotationNode(
        node_id="child-eth",
        parent_node_id="root-usd",
        depth=1,
        asset="ETH",
        quantity_total=Decimal("60"),
        quantity_free=Decimal("60"),
        status=RotationNodeStatus.OPEN,  # NOT PLANNED
    )
    tree = RotationTreeState(nodes=(parent, child))
    original_tree = tree

    tree = cancel_planned_node(tree, "child-eth")

    # No changes — node is not PLANNED
    assert tree is original_tree
