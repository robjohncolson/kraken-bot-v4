"""Textual app tests — screen navigation and rendering."""
from __future__ import annotations

import asyncio

from tui.app import KrakenCockpit
from tui.screens.overview import OverviewScreen
from tui.screens.positions import PositionsScreen
from tui.screens.beliefs import BeliefsScreen
from tui.screens.orders import OrdersScreen
from tui.screens.reconciliation import ReconciliationScreen
from tui.screens.logs import LogsScreen
from tui.screens.help import HelpScreen
from tui.state import CockpitState, HealthState, PortfolioState
from tui.widgets.health import HealthWidget
from tui.widgets.portfolio import PortfolioWidget


async def _run_app_test(callback):
    """Helper: launch app, run callback(app, pilot), shut down."""
    app = KrakenCockpit()
    async with app.run_test(size=(120, 40)) as pilot:
        await callback(app, pilot)


def test_app_launches() -> None:
    async def _check(app, pilot):
        assert isinstance(app.screen, OverviewScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_positions() -> None:
    async def _check(app, pilot):
        await pilot.press("2")
        assert isinstance(app.screen, PositionsScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_beliefs() -> None:
    async def _check(app, pilot):
        await pilot.press("3")
        assert isinstance(app.screen, BeliefsScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_orders() -> None:
    async def _check(app, pilot):
        await pilot.press("4")
        assert isinstance(app.screen, OrdersScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_recon() -> None:
    async def _check(app, pilot):
        await pilot.press("5")
        assert isinstance(app.screen, ReconciliationScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_logs() -> None:
    async def _check(app, pilot):
        await pilot.press("6")
        assert isinstance(app.screen, LogsScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_help() -> None:
    async def _check(app, pilot):
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
    asyncio.run(_run_app_test(_check))


def test_back_to_overview() -> None:
    async def _check(app, pilot):
        await pilot.press("3")
        assert isinstance(app.screen, BeliefsScreen)
        await pilot.press("1")
        assert isinstance(app.screen, OverviewScreen)
    asyncio.run(_run_app_test(_check))


def test_toggle_pause() -> None:
    async def _check(app, pilot):
        assert app.state.paused is False
        await pilot.press("p")
        assert app.state.paused is True
        await pilot.press("p")
        assert app.state.paused is False
    asyncio.run(_run_app_test(_check))


def test_refresh_display_with_state() -> None:
    async def _check(app, pilot):
        app.state.health = HealthState(version="1.2.3", uptime_seconds=60)
        app.state.portfolio = PortfolioState(cash_usd="100.50")
        app.state.connected = True
        app._refresh_display()
        # Widgets live on the screen, not the app itself
        screen = app.screen
        assert screen.query_one("#ov-health", HealthWidget) is not None
        assert screen.query_one("#ov-portfolio", PortfolioWidget) is not None
    asyncio.run(_run_app_test(_check))


def test_state_is_read_only_by_design() -> None:
    """Cockpit state has no mutation methods beyond add_event."""
    state = CockpitState()
    assert not hasattr(state, "place_order")
    assert not hasattr(state, "cancel_order")
    assert callable(state.add_event)
