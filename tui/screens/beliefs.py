"""Full-page beliefs screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.beliefs import BeliefsTable
from tui.widgets.status_bar import StatusBar


class BeliefsScreen(Screen):
    DEFAULT_CSS = """
    BeliefsScreen {
        layout: vertical;
    }
    #bs-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield BeliefsTable(id="bs-table")
        yield StatusBar(id="bs-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#bs-table", BeliefsTable).refresh_content(state.beliefs)
            self.query_one("#bs-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
