"""Overview screen — single-page operational summary."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.beliefs import BeliefsTable
from tui.widgets.event_log import EventLogWidget
from tui.widgets.health import HealthWidget
from tui.widgets.orders import OrdersTable
from tui.widgets.portfolio import PortfolioWidget
from tui.widgets.positions import PositionsTable
from tui.widgets.reconciliation import ReconciliationWidget
from tui.widgets.status_bar import StatusBar


class OverviewScreen(Screen):
    """Default landing screen — dense cockpit summary."""

    DEFAULT_CSS = """
    OverviewScreen {
        layout: vertical;
    }

    #ov-main {
        height: 1fr;
    }

    #ov-top {
        layout: horizontal;
        height: auto;
        max-height: 9;
    }
    #ov-top > * {
        width: 1fr;
    }

    #ov-mid {
        layout: horizontal;
        height: auto;
        max-height: 12;
    }
    #ov-mid > * {
        width: 1fr;
    }

    #ov-beliefs {
        height: auto;
        max-height: 10;
    }

    #ov-recon {
        height: auto;
        max-height: 8;
    }

    #ov-log {
        height: 1fr;
        min-height: 4;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="ov-main"):
            with Horizontal(id="ov-top"):
                yield HealthWidget(id="ov-health")
                yield PortfolioWidget(id="ov-portfolio")
            with Horizontal(id="ov-mid"):
                yield PositionsTable(id="ov-positions")
                yield OrdersTable(id="ov-orders")
            yield BeliefsTable(id="ov-beliefs")
            yield ReconciliationWidget(id="ov-recon")
            yield EventLogWidget(id="ov-log")
        yield StatusBar(id="ov-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            self.query_one("#ov-health", HealthWidget).refresh_content(
                state.health, connected=state.connected,
            )
            self.query_one("#ov-portfolio", PortfolioWidget).refresh_content(
                state.portfolio, state.rotation_tree,
            )
            self.query_one("#ov-positions", PositionsTable).refresh_content(state.positions)
            self.query_one("#ov-orders", OrdersTable).refresh_content(state.orders)
            self.query_one("#ov-beliefs", BeliefsTable).refresh_content(state.beliefs)
            self.query_one("#ov-recon", ReconciliationWidget).refresh_content(
                state.reconciliation,
            )
            self.query_one("#ov-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass  # widgets not yet mounted
