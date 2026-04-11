"""Holdings screen — full table of all Kraken holdings."""
from __future__ import annotations

import glob
import re

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.holdings import HoldingsTable
from tui.widgets.status_bar import StatusBar


def _find_latest_brain_report() -> str | None:
    """Find the latest brain_*.md report file."""
    patterns = [
        "state/cc-reviews/brain_*.md",
        "../state/cc-reviews/brain_*.md",
    ]
    for pattern in patterns:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]
    return None


def _parse_holdings_from_report(path: str) -> list[dict]:
    """Parse holdings from the Observe section of a brain report.

    Lines like:
      USD      $  265.89  (qty=265.8893 @ $1.0000)
      LTC      $   40.56  (qty=0.7286 @ $55.6700)
    """
    holdings: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, IOError):
        return holdings

    observe_pat = (
        r"--- Step 2: Observe ---\n(.*?)(?=\n--- Step)"
    )
    observe_match = re.search(observe_pat, content, re.DOTALL)
    if not observe_match:
        return holdings

    observe_block = observe_match.group(1)
    holding_re = re.compile(
        r"^\s+(\S+)\s+\$\s*([\d.,]+)\s+\(qty=([\d.,]+)\s+@\s+\$([\d.,]+)\)",
        re.MULTILINE,
    )
    for m in holding_re.finditer(observe_block):
        holdings.append({
            "asset": m.group(1),
            "usd_value": m.group(2).replace(",", ""),
            "quantity": m.group(3).replace(",", ""),
            "price": m.group(4).replace(",", ""),
        })

    return holdings


def _parse_portfolio_value(path: str) -> str:
    """Extract portfolio value from 'Portfolio: $485.78' line."""
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, IOError):
        return "0"

    m = re.search(r"Portfolio:\s+\$([\d.,]+)", content)
    return m.group(1).replace(",", "") if m else "0"


class HoldingsScreen(Screen):
    """Full-page holdings table."""

    DEFAULT_CSS = """
    HoldingsScreen {
        layout: vertical;
    }
    #hd-table {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield HoldingsTable(id="hd-table")
        yield StatusBar(id="hd-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            # Parse holdings from latest brain report
            report_path = _find_latest_brain_report()

            holdings: list[dict] = []
            portfolio_value = state.portfolio.total_value_usd

            if report_path:
                holdings = _parse_holdings_from_report(report_path)
                pv = _parse_portfolio_value(report_path)
                if pv and pv != "0":
                    portfolio_value = pv

            self.query_one("#hd-table", HoldingsTable).refresh_content(
                holdings, portfolio_value,
            )
            self.query_one("#hd-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass
