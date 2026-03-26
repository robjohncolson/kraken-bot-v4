"""Full-page orders screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.orders import OrdersTable
from tui.widgets.status_bar import StatusBar


class OrdersScreen(Screen):
    DEFAULT_CSS = """
    OrdersScreen {
        layout: vertical;
    }
    #os-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield OrdersTable(id="os-table")
        yield StatusBar(id="os-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#os-table", OrdersTable).refresh_content(state.orders)
            self.query_one("#os-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
