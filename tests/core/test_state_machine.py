from __future__ import annotations

from decimal import Decimal

from core.config import Settings, load_settings
from core.state_machine import NO_ACTIONS, reduce
from core.types import BotState, PriceTick


def _settings() -> Settings:
    return load_settings(
        {
            "KRAKEN_API_KEY": "key",
            "KRAKEN_API_SECRET": "secret",
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_KEY": "supabase-key",
        }
    )


def test_reduce_returns_deterministic_no_op_for_initial_state() -> None:
    state = BotState()
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.12"))

    next_state, actions = reduce(state, event, _settings())

    assert next_state is state
    assert next_state == state
    assert actions == NO_ACTIONS


def test_reduce_is_pure_for_repeated_identical_inputs() -> None:
    state = BotState()
    event = PriceTick(pair="DOGE/USD", price=Decimal("0.12"))
    settings = _settings()

    first_result = reduce(state, event, settings)
    second_result = reduce(state, event, settings)

    assert first_result == second_result
    assert first_result[0] is state
    assert second_result[0] is state
    assert state == BotState()
