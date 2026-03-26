"""Color semantics for the operator cockpit.

Color conveys state, not decoration:
- green:   healthy / bullish / profitable / connected
- red:     unhealthy / bearish / loss / discrepancy / blocked
- yellow:  warning / stale / reconnecting / cooldown
- cyan:    neutral / informational
"""
from __future__ import annotations

from rich.style import Style
from rich.text import Text


# -- Status ------------------------------------------------------------------
HEALTHY = Style(color="green")
UNHEALTHY = Style(color="red")
WARNING = Style(color="yellow")
NEUTRAL = Style(color="cyan")
MUTED = Style(color="bright_black")

# -- Directions --------------------------------------------------------------
BULLISH = Style(color="green", bold=True)
BEARISH = Style(color="red", bold=True)
NEUTRAL_DIR = Style(color="cyan")

# -- P&L ---------------------------------------------------------------------
PROFIT = Style(color="green")
LOSS = Style(color="red")

# -- Connection ---------------------------------------------------------------
CONNECTED_STYLE = Style(color="green", bold=True)
DISCONNECTED_STYLE = Style(color="red", bold=True)
RECONNECTING_STYLE = Style(color="yellow")

# -- Panel titles -------------------------------------------------------------
PANEL_TITLE = Style(color="cyan", bold=True)


def direction_style(direction: str) -> Style:
    d = direction.lower()
    if d == "bullish":
        return BULLISH
    if d == "bearish":
        return BEARISH
    return NEUTRAL_DIR


def pnl_style(value_str: str) -> Style:
    try:
        v = float(value_str)
    except (ValueError, TypeError):
        return NEUTRAL
    if v > 0:
        return PROFIT
    if v < 0:
        return LOSS
    return NEUTRAL


def direction_text(direction: str) -> Text:
    return Text(direction, style=direction_style(direction))


def pnl_text(value_str: str) -> Text:
    style = pnl_style(value_str)
    try:
        v = float(value_str)
        if v > 0:
            return Text(f"+${v:.2f}", style=style)
        if v < 0:
            return Text(f"-${abs(v):.2f}", style=style)
        return Text(f"${v:.2f}", style=style)
    except (ValueError, TypeError):
        return Text(value_str, style=NEUTRAL)


def confidence_text(confidence: float) -> Text:
    if confidence >= 0.7:
        style = HEALTHY
    elif confidence >= 0.4:
        style = WARNING
    else:
        style = MUTED
    return Text(f"{confidence:.2f}", style=style)


def bool_indicator(value: bool, true_text: str = "YES", false_text: str = "No") -> Text:
    if value:
        return Text(true_text, style=UNHEALTHY)
    return Text(false_text, style=HEALTHY)


def format_uptime(seconds: float) -> str:
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
