"""Post-mortem analysis widgets: trade outcomes, patterns, param changes."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.table import Table
from rich.text import Text
from textual.widgets import DataTable, Static

from tui.state import MemoryRow, TradeOutcomeRow
from tui.theme import HEALTHY, MUTED, UNHEALTHY, pnl_text

_UTC = ZoneInfo("UTC")

_OUTCOME_COLUMNS = ("Pair", "Dir", "Entry", "Exit", "P&L", "Exit Reason", "Hours")


def _dir_text(direction: str) -> Text:
    """Short direction label, color-coded."""
    d = direction.lower()
    if d in ("long", "buy"):
        return Text("LONG", style=HEALTHY)
    if d in ("short", "sell"):
        return Text("SHORT", style=UNHEALTHY)
    return Text(direction.upper() if direction else "\u2014", style=MUTED)


class TradeOutcomesTable(DataTable):
    """Table of closed trade outcomes from the post-mortem engine."""

    DEFAULT_CSS = """
    TradeOutcomesTable {
        border: solid $accent;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "TRADE OUTCOMES"
        self.cursor_type = "row"
        self.zebra_stripes = True
        for col in _OUTCOME_COLUMNS:
            self.add_column(col, key=col.lower().replace(" ", "_").replace("&", ""))

    def refresh_content(self, trade_outcomes: list[TradeOutcomeRow]) -> None:
        self.clear()
        if not trade_outcomes:
            self.add_row("\u2014", "", "", "", "", "", "")
            self.border_subtitle = ""
            return

        # Sort by closed_at descending
        sorted_outcomes = sorted(
            trade_outcomes,
            key=lambda t: t.closed_at,
            reverse=True,
        )

        wins = 0
        total_pnl = 0.0
        for row in sorted_outcomes:
            try:
                pnl_val = float(row.net_pnl)
            except (ValueError, TypeError):
                pnl_val = 0.0
            if pnl_val > 0:
                wins += 1
            total_pnl += pnl_val

            hours_str = f"{row.hold_hours:.1f}" if row.hold_hours else "\u2014"

            self.add_row(
                row.pair or "\u2014",
                _dir_text(row.direction),
                row.entry_price,
                row.exit_price,
                pnl_text(row.net_pnl),
                row.exit_reason or "\u2014",
                hours_str,
            )

        n = len(sorted_outcomes)
        win_pct = (wins / n * 100) if n else 0
        self.border_subtitle = (
            f"{n} trades | {wins} wins ({win_pct:.0f}%) | P&L: ${total_pnl:+.2f}"
        )


class PatternsPanel(Static):
    """Aggregated diagnosis patterns from post-mortem memories."""

    DEFAULT_CSS = """
    PatternsPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "PATTERNS"
        self._do_render([])

    def refresh_content(self, postmortems: list[MemoryRow]) -> None:
        self._do_render(postmortems)

    def _do_render(self, postmortems: list[MemoryRow]) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("label", style="bold", width=16)
        tbl.add_column("value")

        if not postmortems:
            tbl.add_row("Diagnoses", "\u2014")
            tbl.add_row("Repeat Losers", "\u2014")
            self.update(tbl)
            return

        # Count diagnosis patterns
        diagnosis_counts: Counter[str] = Counter()
        pair_counts: Counter[str] = Counter()
        for mem in postmortems:
            diag = mem.content.get("diagnosis", "")
            if diag:
                diagnosis_counts[diag] += 1
            if mem.pair:
                pair_counts[mem.pair] += 1

        # Format diagnoses
        if diagnosis_counts:
            parts = [f"{d}({c}x)" for d, c in diagnosis_counts.most_common(6)]
            tbl.add_row("Diagnoses", ", ".join(parts))
        else:
            tbl.add_row("Diagnoses", "\u2014")

        # Repeat losers: pairs appearing 2+ times
        repeat = [f"{p}({c}x)" for p, c in pair_counts.most_common() if c >= 2]
        if repeat:
            tbl.add_row("Repeat Losers", ", ".join(repeat))
        else:
            tbl.add_row("Repeat Losers", "\u2014")

        self.update(tbl)


def _format_time_utc(iso_str: str) -> str:
    """Format ISO timestamp as HH:MM."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_UTC)
        utc = dt.astimezone(_UTC)
        return utc.strftime("%H:%M")
    except (ValueError, TypeError):
        return iso_str[:5] if len(iso_str) >= 5 else iso_str or "\u2014"


class ParamChangesPanel(Static):
    """Recent parameter adjustments from the post-mortem engine."""

    DEFAULT_CSS = """
    ParamChangesPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "PARAM CHANGES"
        self._do_render([])

    def refresh_content(self, param_changes: list[MemoryRow]) -> None:
        self._do_render(param_changes)

    def _do_render(self, param_changes: list[MemoryRow]) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("time", style="dim", width=6)
        tbl.add_column("change")

        if not param_changes:
            tbl.add_row("\u2014", "No parameter changes")
            self.update(tbl)
            return

        # Sort by timestamp descending, limit to 5
        sorted_changes = sorted(
            param_changes,
            key=lambda m: m.timestamp,
            reverse=True,
        )[:5]

        for mem in sorted_changes:
            time_str = _format_time_utc(mem.timestamp)
            content = mem.content
            param = content.get("param", "?")
            old_val = content.get("old", "?")
            new_val = content.get("new", "?")
            reason = content.get("reason", "")
            change_str = f"{param} {old_val}\u2192{new_val}"
            if reason:
                change_str += f" ({reason})"
            tbl.add_row(time_str, change_str)

        self.update(tbl)
