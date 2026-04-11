"""Market map screen — brain pair analysis overview."""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.market_map import MarketMapTable
from tui.widgets.status_bar import StatusBar

_PROJECT_ROOT = Path(__file__).parent.parent.parent


def _find_latest_brain_report() -> str | None:
    """Find the latest brain_*.md report file."""
    reviews_dir = _PROJECT_ROOT / "state" / "cc-reviews"
    matches = sorted(reviews_dir.glob("brain_*.md"))
    if matches:
        return str(matches[-1])
    return None


class MarketMapScreen(Screen):
    """Full-page market map from latest brain report."""

    DEFAULT_CSS = """
    MarketMapScreen {
        layout: vertical;
    }
    #mm-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield MarketMapTable(id="mm-table")
        yield StatusBar(id="mm-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            report_path = _find_latest_brain_report()
            self.query_one("#mm-table", MarketMapTable).refresh_content(report_path)
            self.query_one("#mm-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
