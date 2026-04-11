"""Brain decision log table widget."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from rich.style import Style
from rich.text import Text
from textual.widgets import DataTable

from tui.state import MemoryRow
from tui.theme import HEALTHY, MUTED, UNHEALTHY, WARNING

_UTC = ZoneInfo("UTC")

_COLUMNS = ("Time", "Pair", "Action", "Signals")

_ACTION_STYLES: dict[str, Style] = {
    "buy": HEALTHY,
    "sell": UNHEALTHY,
    "hold": MUTED,
    "exit": WARNING,
}


def _format_time_utc(iso_str: str) -> str:
    """Format ISO timestamp as HH:MM UTC."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        utc = dt.astimezone(_UTC)
        return utc.strftime("%H:%M UTC")
    except (ValueError, TypeError):
        return iso_str[:5] if len(iso_str) >= 5 else iso_str or "\u2014"


def _action_text(action: str) -> Text:
    """Color-coded action label."""
    style = _ACTION_STYLES.get(action.lower(), MUTED)
    return Text(action.upper(), style=style)


def _build_signals_summary(content: dict, action: str) -> str:
    """Build a compact signal summary string from the content dict."""
    if action.lower() == "hold":
        return content.get("reason", "")

    signals = content.get("signals", {})
    if not isinstance(signals, dict):
        return str(signals) if signals else ""

    parts: list[str] = []
    # Standard signal keys with short labels
    label_map = {
        "4h_trend": "4H",
        "4h": "4H",
        "kronos": "Kronos",
        "regime": "Regime",
        "rsi": "RSI",
        "timesfm": "T",
        "hmm": "HMM",
        "macd": "MACD",
        "volume": "Vol",
    }
    for key, val in signals.items():
        label = label_map.get(key.lower(), key)
        parts.append(f"{label}={val}")

    # Append dry_run indicator if present
    if content.get("dry_run"):
        parts.append("DRY")

    return " ".join(parts)


class BrainDecisionTable(DataTable):
    """Tabular display of CC brain decisions from temporal memory."""

    DEFAULT_CSS = """
    BrainDecisionTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "BRAIN DECISIONS"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower())

    def refresh_content(self, decisions: list[MemoryRow]) -> None:
        self.clear()
        if not decisions:
            self.add_row("\u2014", "", "", "")
            self.border_subtitle = ""
            return

        # Sort by timestamp descending (newest first)
        sorted_decisions = sorted(
            decisions,
            key=lambda d: d.timestamp,
            reverse=True,
        )

        for row in sorted_decisions:
            action = row.content.get("action", "") if row.content else ""
            signals_summary = _build_signals_summary(row.content, action)

            self.add_row(
                _format_time_utc(row.timestamp),
                row.pair or "\u2014",
                _action_text(action) if action else Text("\u2014", style=MUTED),
                signals_summary,
            )

        self.border_subtitle = f"{len(sorted_decisions)} decisions in last 48h"
