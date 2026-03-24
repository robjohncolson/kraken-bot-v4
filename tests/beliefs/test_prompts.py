from __future__ import annotations

import pytest

from beliefs.prompts import MARKET_DATA_PLACEHOLDERS, build_belief_prompt


def test_build_belief_prompt_includes_required_sections() -> None:
    prompt = build_belief_prompt(
        pair="DOGE/USD",
        timeframe="1h",
        recent_trade_history_summary=(
            "Two of the last five DOGE/USD longs closed green, but one high-confidence "
            "bullish call failed during a ranging chop."
        ),
    )

    assert isinstance(prompt, str)
    assert "DOGE/USD" in prompt
    assert "1h timeframe" in prompt
    assert "last 5 closed positions" in prompt
    assert "trades table" in prompt
    assert "Recent trade history summary:" in prompt
    assert "high-confidence bullish call failed" in prompt
    assert "confidence score between 0.0 and 1.0" in prompt
    assert "trending, ranging, or unknown" in prompt
    assert "bullish, bearish, or neutral" in prompt

    for placeholder in MARKET_DATA_PLACEHOLDERS:
        assert placeholder in prompt

    for lifecycle_step in (
        "Formation",
        "Consensus",
        "TA confirmation",
        "Grid activation",
        "Staleness",
        "Position closure",
    ):
        assert lifecycle_step in prompt


def test_build_belief_prompt_supports_custom_pair_timeframe_and_lookback() -> None:
    prompt = build_belief_prompt(
        pair="BTC/USD",
        timeframe="4h",
        recent_trade_history_summary="Last BTC/USD short closed profitably after momentum rolled over.",
        last_n_closed_positions=8,
    )

    assert "BTC/USD" in prompt
    assert "4h timeframe" in prompt
    assert "last 8 closed positions" in prompt
    assert "Last BTC/USD short closed profitably" in prompt
    assert '"direction":"bullish|bearish|neutral"' in prompt
    assert '"regime":"trending|ranging|unknown"' in prompt


def test_build_belief_prompt_rejects_non_positive_lookback() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        build_belief_prompt(
            pair="ETH/USD",
            timeframe="15m",
            recent_trade_history_summary="No trades.",
            last_n_closed_positions=0,
        )
