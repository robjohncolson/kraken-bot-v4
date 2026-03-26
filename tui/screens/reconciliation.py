"""Full-page reconciliation screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.reconciliation import ReconciliationWidget
from tui.widgets.status_bar import StatusBar


class ReconciliationScreen(Screen):
    DEFAULT_CSS = """
    ReconciliationScreen {
        layout: vertical;
    }
    #rs-detail {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield ReconciliationWidget(id="rs-detail")
        yield StatusBar(id="rs-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#rs-detail", ReconciliationWidget).refresh_content(
                state.reconciliation,
            )
            self.query_one("#rs-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
