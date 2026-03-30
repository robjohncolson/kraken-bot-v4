from __future__ import annotations

from pathlib import Path

import pytest

from core.config import load_settings
from core.errors import MissingEnvironmentVariableError


REQUIRED_ENV = {
    "KRAKEN_API_KEY": "kraken-key",
    "KRAKEN_API_SECRET": "kraken-secret",
}


def test_load_settings_uses_spec_defaults() -> None:
    settings = load_settings(REQUIRED_ENV)

    assert settings.kraken_tier == "starter"
    assert settings.max_positions == 8
    assert settings.kelly_ci_level == 0.95
    assert settings.grid_headroom_pct == 70
    assert settings.grid_maker_offset_pct == 0.4
    assert settings.local_state_dir == Path("data")
    assert settings.web_port == 8080
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    assert settings.stats_normality_check is True
    assert settings.stats_fail_closed is True
    assert settings.scanner_pair_discovery_ttl_sec == 3600
    assert settings.scanner_max_concurrency == 4
    assert settings.scanner_timeout_sec == 15.0
    assert settings.enable_conditional_tree is False


@pytest.mark.parametrize("missing_name", sorted(REQUIRED_ENV))
def test_load_settings_requires_required_env_vars(missing_name: str) -> None:
    environment = dict(REQUIRED_ENV)
    environment.pop(missing_name)

    with pytest.raises(MissingEnvironmentVariableError) as exc_info:
        load_settings(environment)

    assert exc_info.value.variable_name == missing_name


def test_allowed_pairs_parses_comma_separated() -> None:
    env = {**REQUIRED_ENV, "ALLOWED_PAIRS": "DOGE/USD,BTC/USD"}
    settings = load_settings(env)
    assert settings.allowed_pairs == frozenset({"DOGE/USD", "BTC/USD"})


def test_allowed_pairs_defaults_to_empty() -> None:
    settings = load_settings(REQUIRED_ENV)
    assert settings.allowed_pairs == frozenset()


def test_allowed_pairs_normalizes_kraken_format() -> None:
    env = {**REQUIRED_ENV, "ALLOWED_PAIRS": "xdgusd"}
    settings = load_settings(env)
    assert settings.allowed_pairs == frozenset({"DOGE/USD"})


def test_scanner_settings_parse_from_environment() -> None:
    env = {
        **REQUIRED_ENV,
        "SCANNER_PAIR_DISCOVERY_TTL_SEC": "120",
        "SCANNER_MAX_CONCURRENCY": "2",
        "SCANNER_TIMEOUT_SEC": "4.5",
    }

    settings = load_settings(env)

    assert settings.scanner_pair_discovery_ttl_sec == 120
    assert settings.scanner_max_concurrency == 2
    assert settings.scanner_timeout_sec == 4.5


def test_enable_conditional_tree_parses_from_environment() -> None:
    settings = load_settings({**REQUIRED_ENV, "ENABLE_CONDITIONAL_TREE": "true"})

    assert settings.enable_conditional_tree is True
