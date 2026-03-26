"""Event log tail widget."""
from __future__ import annotations

from datetime import datetime

from textual.widgets import RichLog

_MAX_DISPLAY = 100


class EventLogWidget(RichLog):
    """Scrollable tail of recent bot events."""

    DEFAULT_CSS = """
    EventLogWidget {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "EVENT LOG"
        self.wrap = True
        self.markup = True

    def add_event(self, message: str) -> None:
        """Append a single timestamped event."""
        ts = datetime.now().strftime("%H:%M:%S")
        self.write(f"[dim]{ts}[/] {message}")

    def refresh_content(self, events: list[str]) -> None:
        """Full reload from the state ring buffer."""
        self.clear()
        for ev in events[-_MAX_DISPLAY:]:
            self.write(ev)
