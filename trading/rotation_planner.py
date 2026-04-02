"""Rotation tree planner — scans leaf nodes for child rotation candidates.

Pure function: takes tree state + market context, returns updated tree state
with new planned children. Does NOT execute orders — the runtime handles that.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Final

from core.config import Settings
from core.types import (
    RotationCandidate,
    RotationTreeState,
    ZERO_DECIMAL,
)
from exchange.pair_metadata import PairMetadataCache
from trading.pair_scanner import PairScanner
from trading.rotation_tree import (
    MIN_REMAINING_HOURS,
    add_node,
    build_root_nodes,
    compute_child_allocations,
    entry_base_quantity,
    leaf_nodes,
    live_nodes,
    make_child_node,
    remaining_hours,
    update_node,
)

logger = logging.getLogger(__name__)

PLAN_INTERVAL_SEC: Final[int] = 300  # Re-plan every 5 minutes


class RotationTreePlanner:
    """Plan child rotations for leaf nodes in the rotation tree."""

    def __init__(
        self,
        *,
        settings: Settings,
        pair_scanner: PairScanner,
        pair_metadata: PairMetadataCache | None = None,
    ) -> None:
        self._settings = settings
        self._pair_scanner = pair_scanner
        self._pair_metadata = pair_metadata

    def initialize_roots(
        self,
        balances: dict[str, Decimal],
        prices_usd: dict[str, Decimal] | None = None,
    ) -> RotationTreeState:
        """Build initial tree from portfolio balances."""
        roots = build_root_nodes(
            balances,
            min_value_usd=Decimal(str(self._settings.min_position_usd)),
            prices_usd=prices_usd,
        )
        return RotationTreeState(
            nodes=roots,
            root_node_ids=tuple(r.node_id for r in roots),
        )

    def plan_cycle(
        self,
        tree: RotationTreeState,
        now: datetime,
    ) -> RotationTreeState:
        """Scan leaf nodes and plan child rotations.

        Returns updated tree with new PLANNED children and reserved parent qty.
        """
        if tree.last_planned_at is not None:
            elapsed = (now - tree.last_planned_at).total_seconds()
            if elapsed < PLAN_INTERVAL_SEC:
                return tree

        leaves = leaf_nodes(tree)
        if not leaves:
            return tree

        updated_tree = tree
        child_seq = len(tree.nodes)

        for leaf in leaves:
            if leaf.depth >= tree.max_depth:
                continue
            if leaf.quantity_free < Decimal(str(self._settings.min_position_usd)):
                continue

            # Enforce max children per parent to prevent order churn
            max_children = self._settings.rotation_max_children_per_parent
            existing_children = [
                n for n in live_nodes(updated_tree) if n.parent_node_id == leaf.node_id
            ]
            if len(existing_children) >= max_children:
                continue
            remaining_slots = max_children - len(existing_children)

            # Check remaining time
            hours_left = remaining_hours(leaf, now)
            if hours_left is not None and hours_left < MIN_REMAINING_HOURS:
                continue

            # Scan for candidates
            held_assets = frozenset(n.asset for n in live_nodes(updated_tree))
            try:
                candidates = self._pair_scanner.scan_rotation_candidates(
                    leaf.asset,
                    max_window_hours=hours_left,
                    excluded_assets=held_assets,
                )
            except Exception as exc:
                logger.warning("Rotation scan failed for %s: %s", leaf.asset, exc)
                continue

            if not candidates:
                continue

            # Compute allocations
            allocations = compute_child_allocations(
                leaf,
                candidates,
                min_position=Decimal(str(self._settings.min_position_usd)),
                max_children=remaining_slots,
            )

            # Filter allocations below Kraken ordermin
            if self._pair_metadata is not None:
                filtered: list[tuple[RotationCandidate, Decimal]] = []
                for candidate, qty in allocations:
                    # Convert allocated qty (parent denom) to base for ordermin check
                    if candidate.reference_price_hint and candidate.reference_price_hint > 0:
                        base_qty = entry_base_quantity(
                            candidate.order_side, qty, candidate.reference_price_hint,
                        )
                    else:
                        base_qty = qty
                    if self._pair_metadata.meets_minimum(candidate.pair, base_qty):
                        filtered.append((candidate, qty))
                    else:
                        ordermin = self._pair_metadata.ordermin(candidate.pair)
                        logger.info(
                            "Skipping %s: base_qty=%s below ordermin=%s",
                            candidate.pair, base_qty, ordermin,
                        )
                allocations = filtered

            if not allocations:
                continue

            # Create child nodes and reserve parent qty
            reserved = ZERO_DECIMAL
            for candidate, qty in allocations:
                child = make_child_node(leaf, candidate, qty, now, child_seq)
                updated_tree = add_node(updated_tree, child)
                reserved += qty
                child_seq += 1
                logger.info(
                    "Planned rotation: %s → %s via %s (conf=%.2f, qty=%s, window=%.1fh)",
                    candidate.from_asset, candidate.to_asset, candidate.pair,
                    candidate.confidence, qty, candidate.estimated_window_hours,
                )

            # Reserve parent quantity
            if reserved > 0:
                new_free = max(ZERO_DECIMAL, leaf.quantity_free - reserved)
                new_reserved = leaf.quantity_reserved + reserved
                updated_tree = update_node(
                    updated_tree,
                    leaf.node_id,
                    quantity_free=new_free,
                    quantity_reserved=new_reserved,
                )

        from dataclasses import replace
        return replace(updated_tree, last_planned_at=now)


__all__ = ["RotationTreePlanner"]
