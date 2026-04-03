"""Rotation tree table widget."""
from __future__ import annotations

from rich.style import Style
from rich.text import Text
from textual.widgets import DataTable

from tui.state import RotationNodeRow, RotationTreeState
from tui.theme import HEALTHY, MUTED, NEUTRAL, UNHEALTHY, WARNING, confidence_text

_COLUMNS = ("Node", "Asset", "Qty", "Free", "Status", "Pair", "Side", "Conf", "Deadline", "P&L")

_STATUS_STYLES: dict[str, Style] = {
    "open": HEALTHY,
    "planned": NEUTRAL,
    "closing": WARNING,
    "closed": MUTED,
    "expired": MUTED,
    "cancelled": UNHEALTHY,
}


def _indent_node_id(node_id: str, depth: int) -> Text:
    prefix = "  " * depth
    if depth == 0:
        return Text(f"{prefix}{node_id}", style=Style(bold=True))
    return Text(f"{prefix}{node_id}")


def _status_text(status: str) -> Text:
    style = _STATUS_STYLES.get(status, MUTED)
    return Text(status.upper(), style=style)


class RotationTreeTable(DataTable):
    """Tabular display of rotation tree nodes in hierarchy order."""

    DEFAULT_CSS = """
    RotationTreeTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "ROTATION TREE"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower())

    def refresh_content(self, tree: RotationTreeState) -> None:
        self.clear()
        if not tree.nodes:
            self.add_row("\u2014", "", "", "", "disabled", "", "", "", "", "")
            self.border_subtitle = ""
            return

        # Build parent→children map for ordered display
        children_map: dict[str, list[RotationNodeRow]] = {}
        roots: list[RotationNodeRow] = []
        for node in tree.nodes:
            if not node.parent_node_id or node.node_id in tree.root_node_ids:
                roots.append(node)
            else:
                children_map.setdefault(node.parent_node_id, []).append(node)

        # DFS traversal for tree display
        stack: list[RotationNodeRow] = list(reversed(roots))
        while stack:
            node = stack.pop()
            if node.status == "cancelled":
                continue
            pnl_display = "\u2014"
            if node.realized_pnl:
                try:
                    pnl_val = float(node.realized_pnl)
                    style = HEALTHY if pnl_val >= 0 else UNHEALTHY
                    pnl_display = Text(f"{pnl_val:+.4f}", style=style)
                except ValueError:
                    pass

            self.add_row(
                _indent_node_id(node.node_id, node.depth),
                node.asset,
                node.quantity_total,
                node.quantity_free,
                _status_text(node.status),
                node.entry_pair or "\u2014",
                node.order_side.upper() if node.order_side else "\u2014",
                confidence_text(node.confidence),
                node.deadline_at[:16] if node.deadline_at else "\u2014",
                pnl_display,
            )
            # Push children in reverse so they come out in order
            for child in reversed(children_map.get(node.node_id, [])):
                stack.append(child)

        # Summary footer via border subtitle
        subtitle = (
            f"Open: {tree.open_count} | Closed: {tree.closed_count} | "
            f"Deployed: {tree.total_deployed} | P&L: {tree.total_realized_pnl}"
        )
        # Append last rotation event if available
        if tree.rotation_events:
            last = tree.rotation_events[-1]
            subtitle += f" | Last: {last.event_type} {last.pair}"
        self.border_subtitle = subtitle
