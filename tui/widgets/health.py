"""Health / heartbeat status widget."""
from __future__ import annotations

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from tui.state import HealthState
from tui.theme import CONNECTED_STYLE, DISCONNECTED_STYLE, format_uptime


class HealthWidget(Static):
    """Compact health summary panel."""

    DEFAULT_CSS = """
    HealthWidget {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "HEALTH"
        self._do_render(HealthState(), connected=False)

    def refresh_content(self, health: HealthState, *, connected: bool = False) -> None:
        self._do_render(health, connected=connected)

    def _do_render(self, health: HealthState, *, connected: bool) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=10)
        tbl.add_column("value")

        if connected:
            tbl.add_row("API", Text("Connected", style=CONNECTED_STYLE))
        else:
            tbl.add_row("API", Text("Disconnected", style=DISCONNECTED_STYLE))

        tbl.add_row("Version", health.version or "\u2014")
        tbl.add_row("Uptime", format_uptime(health.uptime_seconds))
        if health.phase_name:
            tbl.add_row("Phase", f"{health.phase_name} ({health.phase_status})")
        else:
            tbl.add_row("Phase", "\u2014")

        self.update(tbl)
