"""Full-page post-mortem analysis screen."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.postmortem import (
    ParamChangesPanel,
    PatternsPanel,
    TradeOutcomesTable,
)
from tui.widgets.status_bar import StatusBar


class PostMortemScreen(Screen):
    DEFAULT_CSS = """
    PostMortemScreen {
        layout: vertical;
    }
    #pm-main {
        height: 1fr;
    }
    #pm-outcomes {
        height: 1fr;
    }
    #pm-patterns {
        height: auto;
    }
    #pm-params {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="pm-main"):
            yield TradeOutcomesTable(id="pm-outcomes")
            yield PatternsPanel(id="pm-patterns")
            yield ParamChangesPanel(id="pm-params")
        yield StatusBar(id="pm-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#pm-outcomes", TradeOutcomesTable).refresh_content(
                state.trade_outcomes,
            )
            self.query_one("#pm-patterns", PatternsPanel).refresh_content(
                state.postmortems,
            )
            self.query_one("#pm-params", ParamChangesPanel).refresh_content(
                state.param_changes,
            )
            self.query_one("#pm-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
