"""Orders table widget."""
from __future__ import annotations

from textual.widgets import DataTable

from tui.state import OrderRow

_COLUMNS = ("ID", "Pair", "Side", "Type", "Status", "Qty", "Filled", "Price")


class OrdersTable(DataTable):
    """Open and pending orders."""

    DEFAULT_CSS = """
    OrdersTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "ORDERS"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower())

    def refresh_content(self, orders: list[OrderRow]) -> None:
        self.clear()
        if not orders:
            self.add_row("\u2014", "", "", "", "", "", "", "")
            return
        for o in orders:
            display_id = o.order_id or o.client_order_id or "\u2014"
            self.add_row(
                display_id,
                o.pair,
                o.side,
                o.order_type or o.kind or "\u2014",
                o.status,
                o.quantity,
                o.filled_quantity,
                o.limit_price or "\u2014",
            )
