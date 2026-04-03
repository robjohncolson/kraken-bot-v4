"""Tests for pruning cancelled nodes from the TUI rotation tree."""
from __future__ import annotations

from unittest.mock import MagicMock

from tui.state import RotationNodeRow, RotationTreeState
from tui.widgets.rotation_tree import RotationTreeTable


def _make_node(
    node_id: str,
    status: str = "open",
    parent_node_id: str = "",
    depth: int = 0,
    asset: str = "DOGE",
) -> RotationNodeRow:
    return RotationNodeRow(
        node_id=node_id,
        parent_node_id=parent_node_id,
        depth=depth,
        asset=asset,
        status=status,
    )


def _make_tree(nodes: list[RotationNodeRow], root_ids: list[str] | None = None) -> RotationTreeState:
    if root_ids is None:
        root_ids = [n.node_id for n in nodes if not n.parent_node_id]
    return RotationTreeState(
        nodes=nodes,
        root_node_ids=root_ids,
        open_count=sum(1 for n in nodes if n.status == "open"),
        closed_count=sum(1 for n in nodes if n.status == "closed"),
    )


def _run_refresh(tree: RotationTreeState) -> list[str]:
    """Run refresh_content on a mock table, return list of rendered node_ids."""
    rows: list[tuple] = []

    table = MagicMock(spec=RotationTreeTable)
    table.add_row = lambda *cells, **kw: rows.append(cells)
    table.clear = MagicMock()

    # Call the unbound method with our mock as self
    RotationTreeTable.refresh_content(table, tree)

    # Extract node_id from Rich Text in first column
    return [str(row[0]).strip() for row in rows]


class TestPruneCancelledNodes:
    """Cancelled nodes must be excluded from TUI rendering."""

    def test_cancelled_nodes_excluded(self):
        nodes = [
            _make_node("R1", status="open"),
            _make_node("C1", status="cancelled", parent_node_id="R1", depth=1),
            _make_node("C2", status="open", parent_node_id="R1", depth=1),
        ]
        rendered = _run_refresh(_make_tree(nodes, root_ids=["R1"]))

        assert "R1" in rendered
        assert "C2" in rendered
        assert "C1" not in rendered

    def test_non_cancelled_statuses_display(self):
        statuses = ["open", "planned", "closing", "closed", "expired"]
        nodes = [_make_node(f"N-{s}", status=s) for s in statuses]
        rendered = _run_refresh(_make_tree(nodes))

        for s in statuses:
            assert f"N-{s}" in rendered, f"Status '{s}' should be rendered"

    def test_parent_with_all_cancelled_children_still_renders(self):
        nodes = [
            _make_node("R1", status="open"),
            _make_node("C1", status="cancelled", parent_node_id="R1", depth=1),
            _make_node("C2", status="cancelled", parent_node_id="R1", depth=1),
        ]
        rendered = _run_refresh(_make_tree(nodes, root_ids=["R1"]))

        assert "R1" in rendered
        assert len(rendered) == 1, "Only the parent should render; all children are cancelled"
