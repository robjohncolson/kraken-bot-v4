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


@pytest.mark.parametrize("missing_name", sorted(REQUIRED_ENV))
def test_load_settings_requires_required_env_vars(missing_name: str) -> None:
    environment = dict(REQUIRED_ENV)
    environment.pop(missing_name)

    with pytest.raises(MissingEnvironmentVariableError) as exc_info:
        load_settings(environment)

    assert exc_info.value.variable_name == missing_name
