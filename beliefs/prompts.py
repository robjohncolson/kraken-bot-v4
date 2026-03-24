from __future__ import annotations

from typing import Final


MARKET_DATA_PLACEHOLDERS: Final[tuple[str, ...]] = (
    "{{market_data_summary}}",
    "{{indicator_snapshot}}",
    "{{order_book_context}}",
    "{{recent_candles}}",
)


def build_belief_prompt(
    pair: str,
    timeframe: str,
    recent_trade_history_summary: str,
    last_n_closed_positions: int = 5,
) -> str:
    """Build the plain-text prompt used by belief sources for one pair."""
    if last_n_closed_positions < 1:
        raise ValueError("last_n_closed_positions must be at least 1")

    trade_history_summary = (
        recent_trade_history_summary.strip()
        or "No recent closed-position summary was provided."
    )
    market_context = "\n".join(
        f"- {placeholder}" for placeholder in MARKET_DATA_PLACEHOLDERS
    )

    return (
        f"You are forming an independent trading belief for {pair} on the {timeframe} timeframe.\n\n"
        "SPEC.md belief lifecycle reference:\n"
        "1. Formation: analyze the pair independently.\n"
        "2. Consensus: this belief will be combined with other sources in a 2/3 agreement check.\n"
        "3. TA confirmation: Bollinger Bands, EMA crossovers, and related indicators confirm the regime call.\n"
        "4. Grid activation: if the consensus regime is ranging, grid trading may be activated on the pair.\n"
        "5. Staleness: beliefs expire after the configured stale window and must be refreshed.\n"
        "6. Position closure: if consensus flips or dissolves, stop new grid entries and unwind the trade.\n\n"
        "Market data context placeholders:\n"
        f"{market_context}\n\n"
        "Database-as-memory instruction:\n"
        f"Before forming a belief about {pair}, review the last {last_n_closed_positions} closed positions "
        "on this pair from the trades table. Note where prior predictions diverged from outcomes and "
        "use those lessons to calibrate confidence.\n\n"
        "Recent trade history summary:\n"
        f"{trade_history_summary}\n\n"
        "Task:\n"
        f"- Produce a directional belief for {pair} on the {timeframe} timeframe.\n"
        "- Choose exactly one direction: bullish, bearish, or neutral.\n"
        "- Provide a confidence score between 0.0 and 1.0.\n"
        "- Classify the regime as trending, ranging, or unknown.\n"
        "- Give brief reasoning grounded in the market context and the reviewed closed-position history.\n\n"
        "Return format:\n"
        '{"direction":"bullish|bearish|neutral","confidence":0.0,'
        '"regime":"trending|ranging|unknown","reasoning":"..."}'
    )


__all__ = ["MARKET_DATA_PLACEHOLDERS", "build_belief_prompt"]
