"""Kraken Bot v4 — TUI Operator Cockpit (read-only)."""
from __future__ import annotations

import asyncio
import os
from datetime import datetime

from textual.app import App
from textual.binding import Binding
from textual import work

from tui.client import DashboardClient
from tui.events import INITIAL_BACKOFF_SEC, MAX_BACKOFF_SEC, read_sse_stream
from tui.state import (
    CockpitState,
    merge_sse_update,
    parse_beliefs,
    parse_health,
    parse_portfolio,
    parse_positions,
    parse_reconciliation,
)

from tui.screens.overview import OverviewScreen
from tui.screens.positions import PositionsScreen
from tui.screens.beliefs import BeliefsScreen
from tui.screens.orders import OrdersScreen
from tui.screens.reconciliation import ReconciliationScreen
from tui.screens.logs import LogsScreen
from tui.screens.help import HelpScreen


class KrakenCockpit(App):
    """Read-only operator cockpit for kraken-bot-v4."""

    TITLE = "Kraken Bot v4 \u2014 Operator Cockpit"
    SUB_TITLE = "read-only"

    CSS = """
    Screen {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("1", "show_overview", "Overview", key_display="1"),
        Binding("2", "show_positions", "Positions", key_display="2"),
        Binding("3", "show_beliefs", "Beliefs", key_display="3"),
        Binding("4", "show_orders", "Orders", key_display="4"),
        Binding("5", "show_recon", "Recon", key_display="5"),
        Binding("6", "show_logs", "Logs", key_display="6"),
        Binding("question_mark", "show_help", "Help", key_display="?"),
        Binding("r", "manual_refresh", "Refresh", key_display="r"),
        Binding("p", "toggle_pause", "Pause", key_display="p"),
        Binding("left_square_bracket", "prev_pair", "Prev", key_display="["),
        Binding("right_square_bracket", "next_pair", "Next", key_display="]"),
        Binding("slash", "filter_jump", "Filter", key_display="/"),
        Binding("q", "quit", "Quit", key_display="q"),
    ]

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        base_url = os.environ.get("TUI_BASE_URL", "http://127.0.0.1:58392")
        self.state = CockpitState()
        self._client = DashboardClient(base_url=base_url)
        self._current_screen_name = "overview"
        self._pair_index = 0

    # -- lifecycle -----------------------------------------------------------

    def on_mount(self) -> None:
        self.install_screen(OverviewScreen(), name="overview")
        self.install_screen(PositionsScreen(), name="positions")
        self.install_screen(BeliefsScreen(), name="beliefs")
        self.install_screen(OrdersScreen(), name="orders")
        self.install_screen(ReconciliationScreen(), name="reconciliation")
        self.install_screen(LogsScreen(), name="logs")
        self.install_screen(HelpScreen(), name="help")
        self.push_screen("overview")
        self._fetch_snapshot()
        self._run_sse()

    # -- background workers --------------------------------------------------

    @work(exclusive=True, group="snapshot")
    async def _fetch_snapshot(self) -> None:
        try:
            snapshot = await self._client.fetch_snapshot()
            self._apply_snapshot(snapshot)
            self.state.connected = True
            self._log_event("Snapshot loaded from dashboard API")
        except Exception as exc:
            self.state.connected = False
            self._log_event(f"Snapshot failed: {exc}")
        self._refresh_display()

    @work(exclusive=True, group="sse")
    async def _run_sse(self) -> None:
        """Subscribe to SSE with exponential backoff reconnect."""
        backoff = INITIAL_BACKOFF_SEC
        while True:
            try:
                connected = False
                async for event_name, data in read_sse_stream(
                    self._client.sse_url, timeout=60,
                ):
                    if not connected:
                        connected = True
                        backoff = INITIAL_BACKOFF_SEC
                        self.state.sse_connected = True
                        self._log_event("SSE connected")
                        self._refresh_display()
                    if not self.state.paused and event_name == "dashboard.update":
                        merge_sse_update(self.state, data)
                        self.state.last_update = datetime.now().strftime("%H:%M:%S")
                        self._refresh_display()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_event(f"SSE: {exc}")

            self.state.sse_connected = False
            self._refresh_display()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SEC)

    # -- state management ----------------------------------------------------

    def _apply_snapshot(self, snapshot: dict) -> None:
        s = self.state
        health = snapshot.get("health")
        if health:
            s.health = parse_health(health)
        portfolio = snapshot.get("portfolio")
        if portfolio:
            s.portfolio = parse_portfolio(portfolio)
        positions = snapshot.get("positions")
        if positions:
            s.positions = parse_positions(positions)
        beliefs = snapshot.get("beliefs")
        if beliefs:
            s.beliefs = parse_beliefs(beliefs)
        reconciliation = snapshot.get("reconciliation")
        if reconciliation:
            s.reconciliation = parse_reconciliation(reconciliation)
        # Orders are not on a dedicated endpoint yet; they come via SSE
        s.last_update = datetime.now().strftime("%H:%M:%S")

    def _refresh_display(self) -> None:
        try:
            screen = self.screen
            if hasattr(screen, "refresh_data"):
                screen.refresh_data(self.state)
        except Exception:
            pass

    def _log_event(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self.state.add_event(f"[dim]{ts}[/] {message}")

    # -- screen switching ----------------------------------------------------

    def _switch_to(self, name: str) -> None:
        if self._current_screen_name == name:
            return
        self._current_screen_name = name
        self.switch_screen(name)
        self.call_later(self._refresh_display)

    # -- actions (wired to BINDINGS) -----------------------------------------

    def action_show_overview(self) -> None:
        self._switch_to("overview")

    def action_show_positions(self) -> None:
        self._switch_to("positions")

    def action_show_beliefs(self) -> None:
        self._switch_to("beliefs")

    def action_show_orders(self) -> None:
        self._switch_to("orders")

    def action_show_recon(self) -> None:
        self._switch_to("reconciliation")

    def action_show_logs(self) -> None:
        self._switch_to("logs")

    def action_show_help(self) -> None:
        self._switch_to("help")

    def action_manual_refresh(self) -> None:
        self._fetch_snapshot()
        self.notify("Refreshing\u2026")

    def action_toggle_pause(self) -> None:
        self.state.paused = not self.state.paused
        label = "paused" if self.state.paused else "resumed"
        self.notify(f"Auto-refresh {label}")
        self._log_event(f"Auto-refresh {label}")
        self._refresh_display()

    def action_prev_pair(self) -> None:
        pairs = self._get_pairs()
        if pairs:
            self._pair_index = (self._pair_index - 1) % len(pairs)
            self.notify(f"Pair: {pairs[self._pair_index]}")

    def action_next_pair(self) -> None:
        pairs = self._get_pairs()
        if pairs:
            self._pair_index = (self._pair_index + 1) % len(pairs)
            self.notify(f"Pair: {pairs[self._pair_index]}")

    def action_filter_jump(self) -> None:
        self.notify("Filter not yet implemented")

    # -- helpers -------------------------------------------------------------

    def _get_pairs(self) -> list[str]:
        return sorted({
            *(p.pair for p in self.state.positions),
            *(b.pair for b in self.state.beliefs),
        })
