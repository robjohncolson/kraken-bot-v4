"""Market map table widget — brain pair analysis overview."""
from __future__ import annotations

import re

from rich.text import Text
from textual.widgets import DataTable

from tui.theme import HEALTHY, MUTED, NEUTRAL, UNHEALTHY, WARNING

_COLUMNS = ("Pair", "Regime", "Gate", "RSI", "4H", "Kronos", "Score", "Bar")

# Regex to parse analysis lines from brain reports:
#   SOL/USD    V gate=1.00 RSI= 78.7 4H=UP   Kronos=bullish  => 0.70 [...]
_ANALYSIS_RE = re.compile(
    r"^\s+(\S+)\s+([TVR?])\s+gate=(\S+)\s+RSI=\s*(\S+)\s+4H=(\S+)\s+Kronos=(\S+)\s+=>\s+(\S+)",
    re.MULTILINE,
)

# Regime display styles
_REGIME_STYLES = {
    "T": HEALTHY,     # Trending
    "V": WARNING,     # Volatile
    "R": UNHEALTHY,   # Ranging/Risky
    "?": MUTED,       # Unknown
}

# Kronos display styles
_KRONOS_STYLES = {
    "bullish": HEALTHY,
    "bearish": UNHEALTHY,
    "neutral": NEUTRAL,
}

# Block characters for score bar
_FULL_BLOCK = "\u2588"
_HALF_BLOCK = "\u2584"


def _score_bar(score: float) -> Text:
    """Build a visual bar from score (0.0 to 1.0)."""
    full_blocks = int(score * 10)
    has_half = (score * 10 - full_blocks) >= 0.5
    bar = _FULL_BLOCK * full_blocks
    if has_half:
        bar += _HALF_BLOCK

    if score >= 0.6:
        style = HEALTHY
    elif score >= 0.4:
        style = WARNING
    else:
        style = MUTED

    return Text(bar, style=style)


def _regime_text(regime: str) -> Text:
    style = _REGIME_STYLES.get(regime, MUTED)
    labels = {"T": "TREND", "V": "VOLAT", "R": "RANGE", "?": "???"}
    return Text(labels.get(regime, regime), style=style)


def _kronos_text(kronos: str) -> Text:
    style = _KRONOS_STYLES.get(kronos.lower(), NEUTRAL)
    return Text(kronos.upper(), style=style)


def _trend_text(trend: str) -> Text:
    upper = trend.upper()
    if upper == "UP":
        return Text(upper, style=HEALTHY)
    if upper == "DOWN":
        return Text(upper, style=UNHEALTHY)
    return Text(upper, style=NEUTRAL)


class MarketMapTable(DataTable):
    """Tabular display of brain pair analysis from latest report."""

    DEFAULT_CSS = """
    MarketMapTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "MARKET MAP"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _COLUMNS:
            self.add_column(col, key=col.lower())

    def refresh_content(self, report_path: str | None) -> None:
        self.clear()

        if not report_path:
            self.add_row("\u2014", "", "", "", "", "", "", "")
            self.border_subtitle = "No brain report found"
            return

        # Read and parse the report
        try:
            with open(report_path, encoding="utf-8") as f:
                content = f.read()
        except (OSError, IOError):
            self.add_row("\u2014", "", "", "", "", "", "", "")
            self.border_subtitle = "Error reading report"
            return

        entries = []
        for m in _ANALYSIS_RE.finditer(content):
            try:
                score = float(m.group(7))
            except (ValueError, TypeError):
                score = 0.0
            entries.append({
                "pair": m.group(1),
                "regime": m.group(2),
                "gate": m.group(3),
                "rsi": m.group(4),
                "trend_4h": m.group(5),
                "kronos": m.group(6),
                "score": score,
            })

        if not entries:
            self.add_row("\u2014", "", "", "", "", "", "", "")
            self.border_subtitle = "No analysis data in report"
            return

        # Sort by score descending
        entries.sort(key=lambda e: e["score"], reverse=True)

        for e in entries:
            self.add_row(
                e["pair"],
                _regime_text(e["regime"]),
                e["gate"],
                e["rsi"],
                _trend_text(e["trend_4h"]),
                _kronos_text(e["kronos"]),
                Text(
                    f"{e['score']:.2f}",
                    style=(
                        HEALTHY if e["score"] >= 0.6
                        else WARNING if e["score"] >= 0.4
                        else MUTED
                    ),
                ),
                _score_bar(e["score"]),
            )

        # Extract timestamp from report
        ts_pat = (
            r"CC Brain Cycle.*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})"
        )
        ts_match = re.search(ts_pat, content)
        timestamp = ts_match.group(1) if ts_match else "unknown"
        n = len(entries)
        self.border_subtitle = (
            f"{n} pairs analyzed | last cycle: {timestamp}"
        )
