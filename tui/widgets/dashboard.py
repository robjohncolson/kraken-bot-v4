"""Dashboard overview widgets — compact summary panels."""
from __future__ import annotations


from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from tui.theme import (
    HEALTHY,
    MUTED,
    NEUTRAL,
    LOSS,
    WARNING,
    format_uptime,
    pnl_text,
)


class PortfolioPanel(Static):
    """Total value, holdings count, 7d P&L."""

    DEFAULT_CSS = """
    PortfolioPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "PORTFOLIO"
        self._do_render([], [], "0")

    def refresh_content(
        self,
        holdings: list[dict],
        trade_outcomes: list[dict],
        portfolio_value_usd: str,
    ) -> None:
        self._do_render(holdings, trade_outcomes, portfolio_value_usd)

    def _do_render(
        self,
        holdings: list[dict],
        trade_outcomes: list[dict],
        portfolio_value_usd: str,
    ) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=14)
        tbl.add_column("value", justify="right")

        # Total value
        try:
            val = float(portfolio_value_usd)
            tbl.add_row("Total Value", f"${val:,.2f}")
        except (ValueError, TypeError):
            tbl.add_row("Total Value", f"${portfolio_value_usd}")

        # Holdings count
        count = len(holdings) if holdings else 0
        tbl.add_row("Holdings", str(count))

        # 7d P&L from trade outcomes
        pnl_7d = 0.0
        for outcome in trade_outcomes:
            try:
                pnl_7d += float(outcome.get("net_pnl", 0))
            except (ValueError, TypeError):
                pass
        if trade_outcomes:
            tbl.add_row("7d P&L", pnl_text(f"{pnl_7d:.2f}"))
        else:
            tbl.add_row("7d P&L", Text("\u2014", style=MUTED))

        self.update(tbl)


class BrainStatusPanel(Static):
    """Mode, last cycle time, bot uptime."""

    DEFAULT_CSS = """
    BrainStatusPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "BRAIN STATUS"
        self._do_render(None, [])

    def refresh_content(self, health: object | None, decisions: list[dict]) -> None:
        self._do_render(health, decisions)

    def _do_render(self, health: object | None, decisions: list[dict]) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=14)
        tbl.add_column("value")

        # Mode from health phase
        if health is not None:
            phase = getattr(health, "phase_name", "") or ""
            status = getattr(health, "phase_status", "") or ""
            if phase:
                tbl.add_row("Mode", f"{phase} ({status})" if status else phase)
            else:
                tbl.add_row("Mode", Text("\u2014", style=MUTED))

            # Uptime
            uptime = getattr(health, "uptime_seconds", 0.0)
            tbl.add_row("Uptime", format_uptime(uptime))
        else:
            tbl.add_row("Mode", Text("\u2014", style=MUTED))
            tbl.add_row("Uptime", Text("\u2014", style=MUTED))

        # Last cycle = timestamp of most recent decision
        if decisions:
            last = decisions[-1]
            ts = last.get("timestamp", "")
            tbl.add_row("Last Cycle", ts[:19] if ts else Text("\u2014", style=MUTED))
        else:
            tbl.add_row("Last Cycle", Text("\u2014", style=MUTED))

        self.update(tbl)


class TopHoldingsPanel(Static):
    """Top 5 holdings by value with % of portfolio."""

    DEFAULT_CSS = """
    TopHoldingsPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "TOP HOLDINGS"
        self._do_render([], "0")

    def refresh_content(
        self,
        holdings: list[dict],
        portfolio_value_usd: str,
    ) -> None:
        self._do_render(holdings, portfolio_value_usd)

    def _do_render(self, holdings: list[dict], portfolio_value_usd: str) -> None:
        tbl = Table(show_header=True, box=None, padding=(0, 1), expand=True)
        tbl.add_column("Asset", style="bold", width=8)
        tbl.add_column("Value", justify="right", width=10)
        tbl.add_column("%Port", justify="right", width=7)

        if not holdings:
            tbl.add_row(Text("\u2014", style=MUTED), "", "")
            self.update(tbl)
            return

        try:
            total = float(portfolio_value_usd)
        except (ValueError, TypeError):
            total = 0.0

        # Sort by USD value descending and take top 5
        sorted_h = sorted(
            holdings,
            key=lambda h: float(h.get("usd_value", 0)),
            reverse=True,
        )[:5]

        for h in sorted_h:
            asset = h.get("asset", "?")
            try:
                usd_val = float(h.get("usd_value", 0))
            except (ValueError, TypeError):
                usd_val = 0.0
            pct = (usd_val / total * 100) if total > 0 else 0.0
            tbl.add_row(asset, f"${usd_val:,.2f}", f"{pct:.1f}%")

        self.update(tbl)


class LastDecisionPanel(Static):
    """Most recent CC decision with key signals."""

    DEFAULT_CSS = """
    LastDecisionPanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "LAST DECISION"
        self._do_render([])

    def refresh_content(self, decisions: list[dict]) -> None:
        self._do_render(decisions)

    def _do_render(self, decisions: list[dict]) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=10)
        tbl.add_column("value")

        if not decisions:
            tbl.add_row("Action", Text("\u2014", style=MUTED))
            tbl.add_row("Pair", Text("\u2014", style=MUTED))
            tbl.add_row("Score", Text("\u2014", style=MUTED))
            self.update(tbl)
            return

        last = decisions[-1]
        action = last.get("action", "\u2014")
        pair = last.get("pair", "\u2014")
        signals = last.get("signals", {})
        score = signals.get("score", last.get("score", "\u2014"))

        # Action colored by type
        action_upper = str(action).upper()
        if action_upper == "BUY":
            tbl.add_row("Action", Text(action_upper, style=HEALTHY))
        elif action_upper == "SELL":
            tbl.add_row("Action", Text(action_upper, style=LOSS))
        else:
            tbl.add_row("Action", Text(action_upper, style=NEUTRAL))

        tbl.add_row("Pair", pair)

        # Score
        try:
            score_f = float(score)
            style = HEALTHY if score_f >= 0.6 else WARNING if score_f >= 0.4 else MUTED
            tbl.add_row("Score", Text(f"{score_f:.2f}", style=style))
        except (ValueError, TypeError):
            tbl.add_row("Score", str(score))

        # Key signals (show top items from signals dict)
        sig_parts = []
        for k, v in list(signals.items())[:4]:
            if k == "score":
                continue
            sig_parts.append(f"{k}={v}")
        if sig_parts:
            tbl.add_row("Signals", ", ".join(sig_parts))

        self.update(tbl)


class SelfTunePanel(Static):
    """Current thresholds + last param change from brain reports."""

    DEFAULT_CSS = """
    SelfTunePanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.border_title = "SELF-TUNE"
        self._do_render([])

    def refresh_content(self, param_changes: list[dict]) -> None:
        self._do_render(param_changes)

    def _do_render(self, param_changes: list[dict]) -> None:
        tbl = Table(show_header=False, box=None, padding=(0, 1), expand=True)
        tbl.add_column("key", style="bold", width=18)
        tbl.add_column("value")

        if not param_changes:
            tbl.add_row("Status", Text("No tune data", style=MUTED))
            self.update(tbl)
            return

        # Show latest known param values
        latest_params: dict[str, str] = {}
        last_change_str = ""
        for change in param_changes:
            param = change.get("param", "")
            new_val = change.get("new_value", "")
            old_val = change.get("old_value", "")
            reason = change.get("reason", "")
            if param:
                latest_params[param] = str(new_val)
                last_change_str = f"{param}: {old_val} \u2192 {new_val}"
                if reason:
                    last_change_str += f" ({reason})"

        for param_name in ("ENTRY_THRESHOLD", "MAX_POSITION_PCT", "MIN_REGIME_GATE"):
            val = latest_params.get(param_name, "\u2014")
            tbl.add_row(param_name, val)

        if last_change_str:
            tbl.add_row("Last Change", last_change_str)
        else:
            tbl.add_row("Last Change", Text("\u2014", style=MUTED))

        self.update(tbl)
