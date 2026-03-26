"""Positions table widget."""
from __future__ import annotations

from textual.widgets import DataTable

from tui.state import PositionRow
from tui.theme import direction_text, pnl_text

_COLUMNS = ("Pair", "Side", "Qty", "Entry", "Stop", "Target", "Price", "P&L", "Grid")


class PositionsTable(DataTable):
    """Tabular display of open positions."""

    DEFAULT_CSS = """
    PositionsTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "POSITIONS"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower())

    def refresh_content(self, positions: list[PositionRow]) -> None:
        self.clear()
        if not positions:
            self.add_row("\u2014", "", "", "", "", "", "", "", "")
            return
        for pos in positions:
            self.add_row(
                pos.pair,
                direction_text(pos.side),
                pos.quantity,
                pos.entry_price,
                pos.stop_price,
                pos.target_price,
                pos.current_price,
                pnl_text(pos.unrealized_pnl),
                pos.grid_phase or "\u2014",
            )
