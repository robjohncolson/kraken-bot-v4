"""Full-page brain decision log screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.brain_log import BrainDecisionTable
from tui.widgets.status_bar import StatusBar


class BrainLogScreen(Screen):
    DEFAULT_CSS = """
    BrainLogScreen {
        layout: vertical;
    }
    #bl-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield BrainDecisionTable(id="bl-table")
        yield StatusBar(id="bl-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            tbl = self.query_one("#bl-table", BrainDecisionTable)
            tbl.refresh_content(state.decisions)
            self.query_one("#bl-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
