"""Full-page rotation tree screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.rotation_tree import RotationTreeTable
from tui.widgets.status_bar import StatusBar


class RotationTreeScreen(Screen):
    DEFAULT_CSS = """
    RotationTreeScreen {
        layout: vertical;
    }
    #rt-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield RotationTreeTable(id="rt-table")
        yield StatusBar(id="rt-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#rt-table", RotationTreeTable).refresh_content(state.rotation_tree)
            self.query_one("#rt-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
