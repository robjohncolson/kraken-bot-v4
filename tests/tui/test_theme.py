"""Unit tests for tui.theme — color helpers."""
from __future__ import annotations

from tui.theme import (
    BEARISH,
    BULLISH,
    LOSS,
    NEUTRAL_DIR,
    PROFIT,
    bool_indicator,
    confidence_text,
    direction_style,
    direction_text,
    format_uptime,
    pnl_style,
    pnl_text,
)


class TestDirectionStyle:
    def test_bullish(self) -> None:
        assert direction_style("bullish") == BULLISH

    def test_bearish(self) -> None:
        assert direction_style("bearish") == BEARISH

    def test_neutral(self) -> None:
        assert direction_style("neutral") == NEUTRAL_DIR

    def test_case_insensitive(self) -> None:
        assert direction_style("BULLISH") == BULLISH


class TestPnlStyle:
    def test_positive(self) -> None:
        assert pnl_style("3.5") == PROFIT

    def test_negative(self) -> None:
        assert pnl_style("-1.2") == LOSS

    def test_invalid(self) -> None:
        from tui.theme import NEUTRAL
        assert pnl_style("N/A") == NEUTRAL


class TestFormatUptime:
    def test_seconds_only(self) -> None:
        assert format_uptime(45) == "45s"

    def test_minutes(self) -> None:
        assert format_uptime(125) == "2m 5s"

    def test_hours(self) -> None:
        assert format_uptime(3725) == "1h 2m 5s"


class TestBoolIndicator:
    def test_true(self) -> None:
        t = bool_indicator(True)
        assert t.plain == "YES"

    def test_false(self) -> None:
        t = bool_indicator(False)
        assert t.plain == "No"


class TestConfidenceText:
    def test_high(self) -> None:
        t = confidence_text(0.85)
        assert "0.85" in t.plain

    def test_low(self) -> None:
        t = confidence_text(0.2)
        assert "0.20" in t.plain


class TestDirectionText:
    def test_bullish(self) -> None:
        t = direction_text("bullish")
        assert t.plain == "bullish"

    def test_bearish(self) -> None:
        t = direction_text("bearish")
        assert t.plain == "bearish"


class TestPnlText:
    def test_positive(self) -> None:
        t = pnl_text("3.5")
        assert "+$3.50" in t.plain

    def test_negative(self) -> None:
        t = pnl_text("-1.2")
        assert "-$1.20" in t.plain
