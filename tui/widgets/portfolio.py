"""Portfolio summary widget."""
from __future__ import annotations

from rich.table import Table
from textual.widgets import Static

from tui.state import PortfolioState, RotationTreeState


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

    def refresh_content(
        self,
        portfolio: PortfolioState,
        rotation_tree: RotationTreeState | None = None,
    ) -> None:
        self._do_render(portfolio, rotation_tree)

    def _do_render(
        self,
        p: PortfolioState,
        rt: RotationTreeState | None = None,
    ) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=14)
        tbl.add_column("value", justify="right")

        tbl.add_row("Cash USD", f"${p.cash_usd}")
        tbl.add_row("Cash DOGE", p.cash_doge)

        if rt is not None and rt.rotation_tree_value_usd != "0":
            # Show rotation value (may be "N/A", "~123.45", or "123.45")
            val = rt.rotation_tree_value_usd
            display = val if val in ("N/A",) or val.startswith("~") else f"${val}"
            tbl.add_row("Rotation Value", display)
            tot = rt.total_portfolio_value_usd
            tot_display = tot if tot in ("N/A",) or tot.startswith("~") else f"${tot}"
            tbl.add_row("Total Value", tot_display)
        else:
            tbl.add_row("Total Value", f"${p.total_value_usd}")

        tbl.add_row("Exposure", p.directional_exposure)
        tbl.add_row("Max DD", p.max_drawdown)

        self.update(tbl)
