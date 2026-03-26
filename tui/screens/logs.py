"""Full-page event log screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.event_log import EventLogWidget
from tui.widgets.status_bar import StatusBar


class LogsScreen(Screen):
    DEFAULT_CSS = """
    LogsScreen {
        layout: vertical;
    }
    #ls-log {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield EventLogWidget(id="ls-log")
        yield StatusBar(id="ls-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#ls-log", EventLogWidget).refresh_content(state.events)
            self.query_one("#ls-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
