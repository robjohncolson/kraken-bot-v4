"""Help screen — key bindings and status legend."""
from __future__ import annotations

from rich.table import Table
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header, Static

from tui.state import CockpitState


_BINDINGS = (
    ("1", "Dashboard", "Operational summary (default)"),
    ("2", "Holdings", "Exchange balances and holdings"),
    ("3", "Market Map", "Belief matrix and market view"),
    ("4", "Brain Log", "CC brain decisions from temporal memory"),
    ("5", "Post-Mortem", "Trade outcomes, patterns, param changes"),
    ("6", "Rotation Tree", "Rotation tree hierarchy and status"),
    ("?", "Help", "This help screen"),
    ("r", "Refresh", "Manual data refresh from API"),
    ("p", "Pause/Resume", "Toggle live rendering (SSE keeps flowing)"),
    ("[", "Prev Pair", "Select previous pair"),
    ("]", "Next Pair", "Select next pair"),
    ("/", "Filter", "Filter / jump (future)"),
    ("q", "Quit", "Exit the TUI"),
)


class HelpScreen(Screen):
    DEFAULT_CSS = """
    HelpScreen {
        layout: vertical;
    }
    #help-content {
        height: 1fr;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._build(), id="help-content")
        yield Footer()

    def refresh_data(self, _state: CockpitState) -> None:
        pass  # help is static

    @staticmethod
    def _build() -> Table:
        tbl = Table(title="Operator Cockpit Key Bindings", expand=True)
        tbl.add_column("Key", style="bold cyan", width=8)
        tbl.add_column("Action", style="bold", width=16)
        tbl.add_column("Description")

        for key, action, desc in _BINDINGS:
            tbl.add_row(key, action, desc)

        tbl.add_section()
        tbl.add_row("", "Color Legend", "", style="bold")
        tbl.add_row(
            "", "[green]Green[/green]",
            "Healthy / Bullish / Profitable / Connected",
        )
        tbl.add_row(
            "", "[red]Red[/red]",
            "Unhealthy / Bearish / Loss / Discrepancy",
        )
        tbl.add_row(
            "", "[yellow]Yellow[/yellow]",
            "Warning / Stale / Reconnecting / Cooldown",
        )
        tbl.add_row("", "[cyan]Cyan[/cyan]", "Neutral / Informational")

        return tbl
