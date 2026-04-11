"""Dashboard screen — multi-panel overview."""
from __future__ import annotations

import glob
import re

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Footer, Header

from tui.state import CockpitState
from tui.widgets.dashboard import (
    BrainStatusPanel,
    LastDecisionPanel,
    PortfolioPanel,
    SelfTunePanel,
    TopHoldingsPanel,
)
from tui.widgets.status_bar import StatusBar

# Regex to parse TUNE lines from brain reports:
#   TUNE: MAX_POSITION_PCT 0.04 -> 0.05 (fees=84% of wins)
_TUNE_RE = re.compile(
    r"TUNE:\s+(\S+)\s+(\S+)\s*->\s*(\S+)\s*(?:\(([^)]*)\))?"
)

# Regex to parse ENTRY decision lines from brain reports:
#   ENTRY from USD: SOL/USD score=0.70 [4H_trend=+0.20 ...]
_DECISION_RE = re.compile(
    r"(ENTRY|EXIT|HOLD)\s+(?:from\s+\S+:\s+)?(\S+/\S+)\s+score=(\S+)"
    r"(?:\s+\[([^\]]*)\])?"
)

# Regex to parse signal items like "4H_trend=+0.20"
_SIGNAL_RE = re.compile(r"(\S+?)=([+-]?\S+)")


def _parse_brain_report(path: str) -> dict:
    """Parse a brain report file for decisions and param changes."""
    result: dict = {"decisions": [], "param_changes": [], "timestamp": ""}

    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, IOError):
        return result

    # Extract timestamp from header
    ts_match = re.search(r"CC Brain Cycle.*?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", content)
    if ts_match:
        result["timestamp"] = ts_match.group(1)

    # Extract TUNE param changes
    for m in _TUNE_RE.finditer(content):
        result["param_changes"].append({
            "param": m.group(1),
            "old_value": m.group(2),
            "new_value": m.group(3),
            "reason": m.group(4) or "",
        })

    # Extract decisions
    for m in _DECISION_RE.finditer(content):
        signals: dict = {"score": m.group(3)}
        if m.group(4):
            for sm in _SIGNAL_RE.finditer(m.group(4)):
                signals[sm.group(1)] = sm.group(2)
        result["decisions"].append({
            "action": m.group(1).lower(),
            "pair": m.group(2),
            "score": m.group(3),
            "signals": signals,
            "timestamp": result["timestamp"],
        })

    return result


def _parse_holdings_from_report(path: str) -> list[dict]:
    """Parse holdings lines from the Observe section of a brain report.

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

    # Find the Observe section
    observe_pat = (
        r"--- Step 2: Observe ---\n(.*?)(?=\n--- Step)"
    )
    observe_match = re.search(observe_pat, content, re.DOTALL)
    if not observe_match:
        return holdings

    observe_block = observe_match.group(1)
    # Parse individual holding lines
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


def _parse_trade_outcomes_from_report(path: str) -> list[dict]:
    """Parse trade outcomes from the Post-mortem section.

    Lines like:
      PM: AERO/USD lost $0.2747 (stop_loss, 0.4h) — quick_sl_hit
    """
    outcomes: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, IOError):
        return outcomes

    pm_pat = (
        r"--- Step 4: Post-mortem ---\n(.*?)(?=\n--- Step)"
    )
    pm_match = re.search(pm_pat, content, re.DOTALL)
    if not pm_match:
        return outcomes

    pm_block = pm_match.group(1)
    outcome_re = re.compile(
        r"PM:\s+(\S+)\s+(lost|won)\s+\$([\d.]+)"
    )
    for m in outcome_re.finditer(pm_block):
        pnl = float(m.group(3))
        if m.group(2) == "lost":
            pnl = -pnl
        outcomes.append({
            "pair": m.group(1),
            "net_pnl": str(pnl),
        })

    return outcomes


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


class DashboardScreen(Screen):
    """Multi-panel dashboard overview."""

    DEFAULT_CSS = """
    DashboardScreen {
        layout: vertical;
    }

    #db-main {
        height: 1fr;
    }

    #db-top {
        layout: horizontal;
        height: auto;
        max-height: 8;
    }
    #db-top > * {
        width: 1fr;
    }

    #db-mid {
        layout: horizontal;
        height: auto;
        max-height: 12;
    }
    #db-mid > * {
        width: 1fr;
    }

    #db-tune {
        height: auto;
        max-height: 8;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="db-main"):
            with Horizontal(id="db-top"):
                yield PortfolioPanel(id="db-portfolio")
                yield BrainStatusPanel(id="db-brain")
            with Horizontal(id="db-mid"):
                yield TopHoldingsPanel(id="db-holdings")
                yield LastDecisionPanel(id="db-decision")
            yield SelfTunePanel(id="db-tune")
        yield StatusBar(id="db-status")
        yield Footer()

    def refresh_data(self, state: CockpitState) -> None:
        try:
            # Parse latest brain report for enriched data
            report_path = _find_latest_brain_report()

            holdings: list[dict] = []
            decisions: list[dict] = []
            param_changes: list[dict] = []
            trade_outcomes: list[dict] = []
            portfolio_value = "0"

            if report_path:
                parsed = _parse_brain_report(report_path)
                decisions = parsed.get("decisions", [])
                param_changes = parsed.get("param_changes", [])
                holdings = _parse_holdings_from_report(report_path)
                trade_outcomes = _parse_trade_outcomes_from_report(report_path)
                portfolio_value = _parse_portfolio_value(report_path)

            # Fall back to API state if no report data
            if not portfolio_value or portfolio_value == "0":
                portfolio_value = state.portfolio.total_value_usd

            self.query_one("#db-portfolio", PortfolioPanel).refresh_content(
                holdings, trade_outcomes, portfolio_value,
            )
            self.query_one("#db-brain", BrainStatusPanel).refresh_content(
                state.health, decisions,
            )
            self.query_one("#db-holdings", TopHoldingsPanel).refresh_content(
                holdings, portfolio_value,
            )
            self.query_one("#db-decision", LastDecisionPanel).refresh_content(
                decisions,
            )
            self.query_one("#db-tune", SelfTunePanel).refresh_content(
                param_changes,
            )
            self.query_one("#db-status", StatusBar).refresh_content(
                api_connected=state.connected,
                sse_connected=state.sse_connected,
                paused=state.paused,
                last_update=state.last_update,
            )
        except Exception:
            pass  # widgets not yet mounted
