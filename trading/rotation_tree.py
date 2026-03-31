"""Rotation tree helpers — pure functions over RotationTreeState."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from core.types import (
    RotationCandidate,
    RotationNode,
    RotationNodeStatus,
    RotationTreeState,
)

MIN_CONFIDENCE: Final[float] = 0.55
PARENT_DEPLOY_RATIO: Final[Decimal] = Decimal("0.80")
MAX_CHILD_RATIO: Final[Decimal] = Decimal("0.60")
MIN_REMAINING_HOURS: Final[float] = 2.0


def children_of(tree: RotationTreeState, node_id: str) -> tuple[RotationNode, ...]:
    """Return direct children of a node."""
    return tuple(n for n in tree.nodes if n.parent_node_id == node_id)


def live_nodes(tree: RotationTreeState) -> tuple[RotationNode, ...]:
    """Return all nodes that are PLANNED or OPEN."""
    return tuple(
        n for n in tree.nodes
        if n.status in (RotationNodeStatus.PLANNED, RotationNodeStatus.OPEN)
    )


def leaf_nodes(tree: RotationTreeState) -> tuple[RotationNode, ...]:
    """Return live nodes with no live children."""
    live = live_nodes(tree)
    parent_ids = {n.parent_node_id for n in live if n.parent_node_id}
    return tuple(n for n in live if n.node_id not in parent_ids)


def remaining_hours(node: RotationNode, now: datetime) -> float | None:
    """Hours remaining before deadline. None if no deadline."""
    if node.deadline_at is None:
        return None
    delta = (node.deadline_at - now).total_seconds() / 3600
    return max(0.0, delta)


def node_by_id(tree: RotationTreeState, node_id: str) -> RotationNode | None:
    """Find a node by ID."""
    for n in tree.nodes:
        if n.node_id == node_id:
            return n
    return None


def build_root_nodes(
    balances: dict[str, Decimal],
    min_value_usd: Decimal = Decimal("1"),
    prices_usd: dict[str, Decimal] | None = None,
) -> tuple[RotationNode, ...]:
    """Create root nodes from portfolio balances."""
    roots: list[RotationNode] = []
    usd_prices = prices_usd or {}

    for asset, qty in balances.items():
        if qty <= 0:
            continue
        # Skip dust: check USD value if price available
        usd_price = usd_prices.get(asset, Decimal("1") if asset == "USD" else Decimal("0"))
        usd_value = qty * usd_price
        if usd_value < min_value_usd:
            continue

        roots.append(RotationNode(
            node_id=f"root-{asset.lower()}",
            parent_node_id=None,
            depth=0,
            asset=asset,
            quantity_total=qty,
            quantity_free=qty,
            status=RotationNodeStatus.OPEN,
        ))
    return tuple(roots)


def compute_child_allocations(
    parent: RotationNode,
    candidates: tuple[RotationCandidate, ...],
    min_position: Decimal = Decimal("10"),
) -> list[tuple[RotationCandidate, Decimal]]:
    """Compute confidence-weighted allocations for child rotations.

    Returns list of (candidate, allocated_quantity) pairs.
    """
    if parent.quantity_free <= 0 or not candidates:
        return []

    # Score candidates
    scored: list[tuple[RotationCandidate, float]] = []
    for c in candidates:
        raw = max(0.0, c.confidence - MIN_CONFIDENCE) ** 2
        if raw > 0:
            scored.append((c, raw))

    if not scored:
        return []

    total_score = sum(s for _, s in scored)
    allocatable = parent.quantity_free * PARENT_DEPLOY_RATIO

    result: list[tuple[RotationCandidate, Decimal]] = []
    for candidate, score in scored:
        weight = Decimal(str(score / total_score))
        target = allocatable * weight
        capped = min(target, parent.quantity_free * MAX_CHILD_RATIO)
        if capped >= min_position:
            result.append((candidate, capped.quantize(Decimal("0.01"))))

    return result


def make_child_node(
    parent: RotationNode,
    candidate: RotationCandidate,
    allocated_qty: Decimal,
    now: datetime,
    child_seq: int = 0,
) -> RotationNode:
    """Create a child node from a parent + candidate."""
    child_window = candidate.estimated_window_hours
    parent_remaining = remaining_hours(parent, now)
    if parent_remaining is not None:
        child_window = min(child_window, parent_remaining)

    deadline = now + timedelta(hours=child_window)
    if parent.deadline_at is not None:
        deadline = min(deadline, parent.deadline_at)

    return RotationNode(
        node_id=f"{parent.node_id}-{candidate.to_asset.lower()}-{child_seq}",
        parent_node_id=parent.node_id,
        depth=parent.depth + 1,
        asset=candidate.to_asset,
        quantity_total=allocated_qty,
        quantity_free=allocated_qty,
        entry_pair=candidate.pair,
        from_asset=candidate.from_asset,
        order_side=candidate.order_side,
        entry_price=candidate.reference_price_hint,
        opened_at=now,
        deadline_at=deadline,
        window_hours=child_window,
        confidence=candidate.confidence,
        status=RotationNodeStatus.PLANNED,
    )


def close_node(
    tree: RotationTreeState,
    node_id: str,
    status: RotationNodeStatus = RotationNodeStatus.CLOSED,
) -> RotationTreeState:
    """Mark a node as closed/expired. Does NOT cascade to children."""
    nodes = tuple(
        replace(n, status=status) if n.node_id == node_id else n
        for n in tree.nodes
    )
    return replace(tree, nodes=nodes)


def cascade_close(
    tree: RotationTreeState,
    node_id: str,
    status: RotationNodeStatus = RotationNodeStatus.EXPIRED,
) -> RotationTreeState:
    """Close a node and all its descendants (bottom-up)."""
    # Find all descendant IDs
    to_close = _descendants(tree, node_id) | {node_id}
    nodes = tuple(
        replace(n, status=status) if n.node_id in to_close else n
        for n in tree.nodes
    )
    return replace(tree, nodes=nodes)


def expired_nodes(
    tree: RotationTreeState,
    now: datetime,
) -> tuple[RotationNode, ...]:
    """Find live nodes whose deadline has passed."""
    result: list[RotationNode] = []
    for node in live_nodes(tree):
        if node.deadline_at is not None and now >= node.deadline_at:
            result.append(node)
    return tuple(result)


def add_node(tree: RotationTreeState, node: RotationNode) -> RotationTreeState:
    """Add a node to the tree."""
    return replace(tree, nodes=tree.nodes + (node,))


def update_node(tree: RotationTreeState, node_id: str, **kwargs) -> RotationTreeState:
    """Update fields on a specific node."""
    nodes = tuple(
        replace(n, **kwargs) if n.node_id == node_id else n
        for n in tree.nodes
    )
    return replace(tree, nodes=nodes)


def _descendants(tree: RotationTreeState, node_id: str) -> set[str]:
    """Find all descendant node IDs (recursive)."""
    result: set[str] = set()
    queue = [node_id]
    while queue:
        parent = queue.pop()
        for n in tree.nodes:
            if n.parent_node_id == parent and n.node_id not in result:
                result.add(n.node_id)
                queue.append(n.node_id)
    return result


__all__ = [
    "MIN_CONFIDENCE",
    "add_node",
    "build_root_nodes",
    "cascade_close",
    "children_of",
    "close_node",
    "compute_child_allocations",
    "expired_nodes",
    "leaf_nodes",
    "live_nodes",
    "make_child_node",
    "node_by_id",
    "remaining_hours",
    "update_node",
]
