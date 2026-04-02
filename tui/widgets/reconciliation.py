"""Reconciliation summary widget."""
from __future__ import annotations

from rich.table import Table
from textual.widgets import Static

from tui.state import ReconciliationState
from tui.theme import bool_indicator


class ReconciliationWidget(Static):
    """Compact reconciliation status panel."""

    DEFAULT_CSS = """
    ReconciliationWidget {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "RECONCILIATION"
        self._do_render(ReconciliationState())

    def refresh_content(self, recon: ReconciliationState) -> None:
        self._do_render(recon)

    def _do_render(self, r: ReconciliationState) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=16)
        tbl.add_column("value")

        tbl.add_row("Last Check", r.checked_at or "Never")
        tbl.add_row("Discrepancy", bool_indicator(r.discrepancy_detected))
        tbl.add_row("Ghost Positions", str(len(r.ghost_positions)))
        foreign_label = str(len(r.foreign_orders))
        if r.foreign_orders:
            foreign_label += " (Kraken-side orders not placed by bot)"
        tbl.add_row("Foreign Orders", foreign_label)
        tbl.add_row("Untracked", str(len(r.untracked_assets)))
        tbl.add_row("Fee Drift", str(len(r.fee_drift)))

        self.update(tbl)
