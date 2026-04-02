"""Presentation state for the TUI cockpit.

Every parser accepts raw JSON dicts from the dashboard API / SSE and
returns typed dataclass instances.  The TUI never imports core domain
types directly — this module is the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_EVENT_RING = 200


# -- Leaf states --------------------------------------------------------------

@dataclass
class HealthState:
    version: str = ""
    uptime_seconds: float = 0.0
    phase_name: str = ""
    phase_status: str = ""


@dataclass
class PortfolioState:
    cash_usd: str = "0"
    cash_doge: str = "0"
    total_value_usd: str = "0"
    directional_exposure: str = "0"
    max_drawdown: str = "0"


@dataclass
class PositionRow:
    pair: str = ""
    side: str = ""
    quantity: str = "0"
    entry_price: str = "0"
    stop_price: str = "0"
    target_price: str = "0"
    current_price: str = "0"
    unrealized_pnl: str = "0"
    grid_phase: str = ""


@dataclass
class BeliefCell:
    pair: str = ""
    source: str = ""
    direction: str = "neutral"
    confidence: float = 0.0
    regime: str = "unknown"
    updated_at: str = ""


@dataclass
class OrderRow:
    order_id: str = ""
    pair: str = ""
    side: str = ""
    order_type: str = ""
    status: str = ""
    quantity: str = "0"
    filled_quantity: str = "0"
    limit_price: str = ""
    client_order_id: str = ""
    kind: str = ""


@dataclass
class ReconciliationState:
    checked_at: str = ""
    discrepancy_detected: bool = False
    ghost_positions: list[Any] = field(default_factory=list)
    foreign_orders: list[Any] = field(default_factory=list)
    fee_drift: list[Any] = field(default_factory=list)
    untracked_assets: list[Any] = field(default_factory=list)


@dataclass
class RotationNodeRow:
    node_id: str = ""
    parent_node_id: str = ""
    depth: int = 0
    asset: str = ""
    quantity_total: str = "0"
    quantity_free: str = "0"
    status: str = "planned"
    entry_pair: str = ""
    order_side: str = ""
    confidence: float = 0.0
    deadline_at: str = ""
    window_hours: str = ""
    realized_pnl: str = ""


@dataclass
class RotationTreeState:
    nodes: list[RotationNodeRow] = field(default_factory=list)
    root_node_ids: list[str] = field(default_factory=list)
    last_planned_at: str = ""
    total_deployed: str = "0"
    total_realized_pnl: str = "0"
    open_count: int = 0
    closed_count: int = 0
    rotation_tree_value_usd: str = "0"
    total_portfolio_value_usd: str = "0"


# -- Top-level cockpit state --------------------------------------------------

@dataclass
class CockpitState:
    health: HealthState = field(default_factory=HealthState)
    portfolio: PortfolioState = field(default_factory=PortfolioState)
    positions: list[PositionRow] = field(default_factory=list)
    beliefs: list[BeliefCell] = field(default_factory=list)
    orders: list[OrderRow] = field(default_factory=list)
    reconciliation: ReconciliationState = field(default_factory=ReconciliationState)
    rotation_tree: RotationTreeState = field(default_factory=RotationTreeState)
    events: list[str] = field(default_factory=list)
    connected: bool = False
    sse_connected: bool = False
    last_update: str = ""
    paused: bool = False

    def add_event(self, message: str) -> None:
        self.events.append(message)
        if len(self.events) > MAX_EVENT_RING:
            self.events = self.events[-MAX_EVENT_RING:]


# -- Parsers (JSON dict → typed state) ----------------------------------------

def parse_health(data: dict[str, Any]) -> HealthState:
    phase = data.get("phase_status") or {}
    return HealthState(
        version=str(data.get("version", "")),
        uptime_seconds=float(data.get("uptime_seconds", 0)),
        phase_name=str(phase.get("name", "")),
        phase_status=str(phase.get("status", "")),
    )


def parse_portfolio(data: dict[str, Any]) -> PortfolioState:
    return PortfolioState(
        cash_usd=str(data.get("cash_usd", "0")),
        cash_doge=str(data.get("cash_doge", "0")),
        total_value_usd=str(data.get("total_value_usd", "0")),
        directional_exposure=str(data.get("directional_exposure", "0")),
        max_drawdown=str(data.get("max_drawdown", "0")),
    )


def parse_positions(data: dict[str, Any]) -> list[PositionRow]:
    rows: list[PositionRow] = []
    for item in data.get("positions", []):
        pos = item if "pair" in item else item.get("position", {})
        grid = pos.get("grid_state")
        rows.append(PositionRow(
            pair=str(pos.get("pair", "")),
            side=str(pos.get("side", "")),
            quantity=str(pos.get("quantity", "0")),
            entry_price=str(pos.get("entry_price", "0")),
            stop_price=str(pos.get("stop_price", "0")),
            target_price=str(pos.get("target_price", "0")),
            current_price=str(item.get("current_price", pos.get("entry_price", "0"))),
            unrealized_pnl=str(item.get("unrealized_pnl_usd", "0")),
            grid_phase=str(grid.get("phase", "")) if grid else "",
        ))
    return rows


def parse_beliefs(data: dict[str, Any]) -> list[BeliefCell]:
    """Parse beliefs from either grouped-dict (API) or flat-list (SSE) format."""
    cells: list[BeliefCell] = []
    beliefs = data.get("beliefs", {})

    if isinstance(beliefs, dict):
        for pair, sources in beliefs.items():
            if not isinstance(sources, dict):
                continue
            for source, info in sources.items():
                cells.append(BeliefCell(
                    pair=str(pair),
                    source=str(source),
                    direction=str(info.get("direction", "neutral")),
                    confidence=float(info.get("confidence", 0)),
                    regime=str(info.get("regime", "unknown")),
                    updated_at=str(info.get("updated_at") or ""),
                ))
    elif isinstance(beliefs, list):
        for item in beliefs:
            cells.append(BeliefCell(
                pair=str(item.get("pair", "")),
                source=str(item.get("source", "")),
                direction=str(item.get("direction", "neutral")),
                confidence=float(item.get("confidence", 0)),
                regime=str(item.get("regime", "unknown")),
                updated_at=str(item.get("updated_at") or ""),
            ))
    return cells


def parse_orders(data: dict[str, Any]) -> list[OrderRow]:
    rows: list[OrderRow] = []
    for item in data.get("pending_orders", []):
        rows.append(OrderRow(
            pair=str(item.get("pair", "")),
            side=str(item.get("side", "")),
            status="pending",
            quantity=str(item.get("base_qty", "0")),
            filled_quantity=str(item.get("filled_qty", "0")),
            client_order_id=str(item.get("client_order_id", "")),
            kind=str(item.get("kind", "")),
        ))
    for item in data.get("open_orders", []):
        rows.append(OrderRow(
            order_id=str(item.get("order_id", "")),
            pair=str(item.get("pair", "")),
            side=str(item.get("side", "")),
            order_type=str(item.get("order_type", "")),
            status=str(item.get("status", "")),
            quantity=str(item.get("quantity", "0")),
            filled_quantity=str(item.get("filled_quantity", "0")),
            limit_price=str(item.get("limit_price") or ""),
            client_order_id=str(item.get("client_order_id") or ""),
        ))
    return rows


def parse_reconciliation(data: dict[str, Any]) -> ReconciliationState:
    report = data.get("report")
    if report and isinstance(report, dict):
        # Nested format from SSE (jsonable_encoder of ReconciliationSnapshot)
        return ReconciliationState(
            checked_at=str(data.get("checked_at") or ""),
            discrepancy_detected=bool(report.get("discrepancy_detected", False)),
            ghost_positions=list(report.get("ghost_positions", [])),
            foreign_orders=list(report.get("foreign_orders", [])),
            fee_drift=list(report.get("fee_drift", [])),
            untracked_assets=list(report.get("untracked_assets", [])),
        )
    # Flat format from API (_serialize_reconciliation)
    return ReconciliationState(
        checked_at=str(data.get("checked_at") or ""),
        discrepancy_detected=bool(data.get("discrepancy_detected", False)),
        ghost_positions=list(data.get("ghost_positions", [])),
        foreign_orders=list(data.get("foreign_orders", [])),
        fee_drift=list(data.get("fee_drift", [])),
        untracked_assets=list(data.get("untracked_assets", [])),
    )


def parse_rotation_tree(data: dict[str, Any]) -> RotationTreeState:
    raw_nodes = data.get("nodes") or []
    nodes = [
        RotationNodeRow(
            node_id=str(n.get("node_id", "")),
            parent_node_id=str(n.get("parent_node_id") or ""),
            depth=int(n.get("depth", 0)),
            asset=str(n.get("asset", "")),
            quantity_total=str(n.get("quantity_total", "0")),
            quantity_free=str(n.get("quantity_free", "0")),
            status=str(n.get("status", "planned")),
            entry_pair=str(n.get("entry_pair") or ""),
            order_side=str(n.get("order_side") or ""),
            confidence=float(n.get("confidence", 0.0)),
            deadline_at=str(n.get("deadline_at") or ""),
            window_hours=str(n.get("window_hours") or ""),
            realized_pnl=str(n.get("realized_pnl") or ""),
        )
        for n in raw_nodes
    ]
    return RotationTreeState(
        nodes=nodes,
        root_node_ids=list(data.get("root_node_ids") or []),
        last_planned_at=str(data.get("last_planned_at") or ""),
        total_deployed=str(data.get("total_deployed", "0")),
        total_realized_pnl=str(data.get("total_realized_pnl", "0")),
        open_count=int(data.get("open_count", 0)),
        closed_count=int(data.get("closed_count", 0)),
        rotation_tree_value_usd=str(data.get("rotation_tree_value_usd") or "N/A"),
        total_portfolio_value_usd=str(data.get("total_portfolio_value_usd") or "N/A"),
    )


def merge_sse_update(state: CockpitState, data: dict[str, Any]) -> CockpitState:
    """Merge a ``dashboard.update`` SSE payload into the cockpit state."""
    if "health" in data:
        state.health = parse_health(data["health"])
    if "portfolio" in data:
        state.portfolio = parse_portfolio(data["portfolio"])
    if "positions" in data:
        state.positions = parse_positions(data["positions"])
    if "beliefs" in data:
        state.beliefs = parse_beliefs({"beliefs": data["beliefs"]})
    if "reconciliation" in data:
        state.reconciliation = parse_reconciliation(data["reconciliation"])
    if "rotation_tree" in data:
        state.rotation_tree = parse_rotation_tree(data["rotation_tree"])
    if "pending_orders" in data:
        state.orders = parse_orders({"pending_orders": data["pending_orders"]})
    return state
