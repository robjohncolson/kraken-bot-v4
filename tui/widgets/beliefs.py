"""Beliefs / consensus matrix widget."""
from __future__ import annotations

from textual.widgets import DataTable

from tui.state import BeliefCell
from tui.theme import confidence_text, direction_text

_COLUMNS = ("Pair", "Source", "Direction", "Conf", "Regime", "Updated")


class BeliefsTable(DataTable):
    """Matrix view of beliefs by pair and source."""

    DEFAULT_CSS = """
    BeliefsTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "BELIEFS"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower())

    def refresh_content(self, beliefs: list[BeliefCell]) -> None:
        self.clear()
        if not beliefs:
            self.add_row("\u2014", "", "", "", "", "")
            return
        for b in beliefs:
            self.add_row(
                b.pair,
                b.source,
                direction_text(b.direction),
                confidence_text(b.confidence),
                b.regime,
                b.updated_at or "\u2014",
            )
