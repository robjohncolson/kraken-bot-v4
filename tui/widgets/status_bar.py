"""Connection status bar (docks to bottom, above Footer)."""
from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from tui.theme import CONNECTED_STYLE, DISCONNECTED_STYLE, WARNING


class StatusBar(Static):
    """Single-line bar showing API/SSE status, pause state, last update."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        dock: bottom;
        background: $surface;
        color: $text;
        padding: 0 1;
    }
    """

    def refresh_content(
        self,
        *,
        api_connected: bool = False,
        sse_connected: bool = False,
        paused: bool = False,
        last_update: str = "",
    ) -> None:
        parts = Text()

        parts.append("API:", style="bold")
        if api_connected:
            parts.append(" OK ", style=CONNECTED_STYLE)
        else:
            parts.append(" DOWN ", style=DISCONNECTED_STYLE)

        parts.append("| SSE:", style="bold")
        if sse_connected:
            parts.append(" LIVE ", style=CONNECTED_STYLE)
        else:
            parts.append(" OFF ", style=DISCONNECTED_STYLE)

        if paused:
            parts.append("| ")
            parts.append("PAUSED", style=WARNING)

        if last_update:
            parts.append(" | ")
            parts.append(f"Updated {last_update}", style="dim")

        self.update(parts)
