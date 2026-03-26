"""Full-page positions screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.positions import PositionsTable
from tui.widgets.status_bar import StatusBar


class PositionsScreen(Screen):
    DEFAULT_CSS = """
    PositionsScreen {
        layout: vertical;
    }
    #ps-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield PositionsTable(id="ps-table")
        yield StatusBar(id="ps-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#ps-table", PositionsTable).refresh_content(state.positions)
            self.query_one("#ps-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
