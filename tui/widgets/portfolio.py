"""Portfolio summary widget."""
from __future__ import annotations

from rich.table import Table
from textual.widgets import Static

from tui.state import PortfolioState


class PortfolioWidget(Static):
    """Compact portfolio panel."""

    DEFAULT_CSS = """
    PortfolioWidget {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "PORTFOLIO"
        self._do_render(PortfolioState())

    def refresh_content(self, portfolio: PortfolioState) -> None:
        self._do_render(portfolio)

    def _do_render(self, p: PortfolioState) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=10)
        tbl.add_column("value", justify="right")

        tbl.add_row("Cash USD", f"${p.cash_usd}")
        tbl.add_row("Cash DOGE", p.cash_doge)
        tbl.add_row("Total Value", f"${p.total_value_usd}")
        tbl.add_row("Exposure", p.directional_exposure)
        tbl.add_row("Max DD", p.max_drawdown)

        self.update(tbl)
