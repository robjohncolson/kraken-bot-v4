"""Textual app tests — screen navigation and rendering."""
from __future__ import annotations

import asyncio

from tui.app import KrakenCockpit
from tui.screens.dashboard import DashboardScreen
from tui.screens.holdings import HoldingsScreen
from tui.screens.market_map import MarketMapScreen
from tui.screens.brain_log import BrainLogScreen
from tui.screens.postmortem import PostMortemScreen
from tui.screens.rotation_tree import RotationTreeScreen
from tui.screens.help import HelpScreen
from tui.state import CockpitState, HealthState, PortfolioState


async def _run_app_test(callback):
    """Helper: launch app, run callback(app, pilot), shut down."""
    app = KrakenCockpit()
    async with app.run_test(size=(120, 40)) as pilot:
        await callback(app, pilot)


def test_app_launches() -> None:
    async def _check(app, pilot):
        assert isinstance(app.screen, DashboardScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_holdings() -> None:
    async def _check(app, pilot):
        await pilot.press("2")
        assert isinstance(app.screen, HoldingsScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_market_map() -> None:
    async def _check(app, pilot):
        await pilot.press("3")
        assert isinstance(app.screen, MarketMapScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_brain_log() -> None:
    async def _check(app, pilot):
        await pilot.press("4")
        assert isinstance(app.screen, BrainLogScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_postmortem() -> None:
    async def _check(app, pilot):
        await pilot.press("5")
        assert isinstance(app.screen, PostMortemScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_rotation_tree() -> None:
    async def _check(app, pilot):
        await pilot.press("6")
        assert isinstance(app.screen, RotationTreeScreen)
    asyncio.run(_run_app_test(_check))


def test_screen_switch_help() -> None:
    async def _check(app, pilot):
        await pilot.press("question_mark")
        assert isinstance(app.screen, HelpScreen)
    asyncio.run(_run_app_test(_check))


def test_back_to_dashboard() -> None:
    async def _check(app, pilot):
        await pilot.press("3")
        assert isinstance(app.screen, MarketMapScreen)
        await pilot.press("1")
        assert isinstance(app.screen, DashboardScreen)
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
        # Dashboard screen should have its panels
        screen = app.screen
        assert isinstance(screen, DashboardScreen)
    asyncio.run(_run_app_test(_check))


def test_state_is_read_only_by_design() -> None:
    """Cockpit state has no mutation methods beyond add_event."""
    state = CockpitState()
    assert not hasattr(state, "place_order")
    assert not hasattr(state, "cancel_order")
    assert callable(state.add_event)
