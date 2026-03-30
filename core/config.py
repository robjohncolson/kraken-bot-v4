from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from core.errors import InvalidEnvironmentVariableError, MissingEnvironmentVariableError
from exchange.symbols import normalize_pair

DEFAULT_KRAKEN_TIER = "starter"
DEFAULT_MAX_POSITIONS = 8
DEFAULT_MAX_SAME_SIDE_PCT = 60
DEFAULT_MAX_SINGLE_PAIR_PCT = 15
DEFAULT_MAX_DRAWDOWN_SOFT_PCT = 10
DEFAULT_MAX_DRAWDOWN_HARD_PCT = 15
DEFAULT_KELLY_CI_LEVEL = 0.95
DEFAULT_MIN_POSITION_USD = 10
DEFAULT_MAX_POSITION_USD = 100
DEFAULT_STOP_PCT = 5
DEFAULT_TARGET_PCT = 10
DEFAULT_GRID_HEADROOM_PCT = 70
DEFAULT_GRID_PROFIT_REDIST_INTERVAL_SEC = 3600
DEFAULT_GRID_MAKER_OFFSET_PCT = 0.4
DEFAULT_BELIEF_STALE_HOURS = 4
DEFAULT_BELIEF_CONSENSUS_THRESHOLD = 2
DEFAULT_REENTRY_COOLDOWN_HOURS = 24
DEFAULT_LOCAL_STATE_DIR = Path("./data")
DEFAULT_SQLITE_PATH = Path("./data/bot.db")
DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 8080
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 3
DEFAULT_CIRCUIT_BREAKER_WINDOW_SEC = 120
DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SEC = 300
DEFAULT_RECONCILE_INTERVAL_SEC = 300
DEFAULT_STATS_MIN_SAMPLE_SIZE = 30
DEFAULT_STATS_NORMALITY_CHECK = True
DEFAULT_STATS_FAIL_CLOSED = True
DEFAULT_READ_ONLY_EXCHANGE = True
DEFAULT_DISABLE_ORDER_MUTATIONS = True
DEFAULT_STARTUP_RECONCILE_ONLY = True
DEFAULT_SCANNER_PAIR_DISCOVERY_TTL_SEC = 3600
DEFAULT_SCANNER_MAX_CONCURRENCY = 4
DEFAULT_SCANNER_TIMEOUT_SEC = 15.0
DEFAULT_ENABLE_CONDITIONAL_TREE = False

ALLOWED_KRAKEN_TIERS = frozenset({"starter", "intermediate", "pro"})
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})
REQUIRED_ENV_VARS = (
    "KRAKEN_API_KEY",
    "KRAKEN_API_SECRET",
)


@dataclass(frozen=True, slots=True)
class Settings:
    kraken_api_key: str
    kraken_api_secret: str
    kraken_tier: str
    sqlite_path: Path
    web_host: str
    supabase_url: str | None
    supabase_key: str | None
    max_positions: int
    max_same_side_pct: int
    max_single_pair_pct: int
    max_drawdown_soft_pct: int
    max_drawdown_hard_pct: int
    kelly_ci_level: float
    min_position_usd: int
    max_position_usd: int
    default_stop_pct: int
    default_target_pct: int
    grid_headroom_pct: int
    grid_profit_redist_interval_sec: int
    grid_maker_offset_pct: float
    belief_stale_hours: int
    belief_consensus_threshold: int
    reentry_cooldown_hours: int
    local_state_dir: Path
    web_port: int
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    circuit_breaker_threshold: int
    circuit_breaker_window_sec: int
    circuit_breaker_cooldown_sec: int
    reconcile_interval_sec: int
    stats_min_sample_size: int
    stats_normality_check: bool
    stats_fail_closed: bool
    read_only_exchange: bool
    disable_order_mutations: bool
    startup_reconcile_only: bool
    allowed_pairs: frozenset[str]
    scanner_pair_discovery_ttl_sec: int
    scanner_max_concurrency: int
    scanner_timeout_sec: float
    enable_conditional_tree: bool


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    return Settings(
        kraken_api_key=_read_required(env, "KRAKEN_API_KEY"),
        kraken_api_secret=_read_required(env, "KRAKEN_API_SECRET"),
        kraken_tier=_read_choice(env, "KRAKEN_TIER", DEFAULT_KRAKEN_TIER, ALLOWED_KRAKEN_TIERS),
        sqlite_path=_read_path(env, "SQLITE_PATH", DEFAULT_SQLITE_PATH),
        web_host=_read_optional(env, "WEB_HOST") or DEFAULT_WEB_HOST,
        supabase_url=_read_optional(env, "SUPABASE_URL"),
        supabase_key=_read_optional(env, "SUPABASE_KEY"),
        max_positions=_read_int(env, "MAX_POSITIONS", DEFAULT_MAX_POSITIONS),
        max_same_side_pct=_read_int(env, "MAX_SAME_SIDE_PCT", DEFAULT_MAX_SAME_SIDE_PCT),
        max_single_pair_pct=_read_int(env, "MAX_SINGLE_PAIR_PCT", DEFAULT_MAX_SINGLE_PAIR_PCT),
        max_drawdown_soft_pct=_read_int(
            env, "MAX_DRAWDOWN_SOFT_PCT", DEFAULT_MAX_DRAWDOWN_SOFT_PCT
        ),
        max_drawdown_hard_pct=_read_int(
            env, "MAX_DRAWDOWN_HARD_PCT", DEFAULT_MAX_DRAWDOWN_HARD_PCT
        ),
        kelly_ci_level=_read_float(env, "KELLY_CI_LEVEL", DEFAULT_KELLY_CI_LEVEL),
        min_position_usd=_read_int(env, "MIN_POSITION_USD", DEFAULT_MIN_POSITION_USD),
        max_position_usd=_read_int(env, "MAX_POSITION_USD", DEFAULT_MAX_POSITION_USD),
        default_stop_pct=_read_int(env, "DEFAULT_STOP_PCT", DEFAULT_STOP_PCT),
        default_target_pct=_read_int(env, "DEFAULT_TARGET_PCT", DEFAULT_TARGET_PCT),
        grid_headroom_pct=_read_int(env, "GRID_HEADROOM_PCT", DEFAULT_GRID_HEADROOM_PCT),
        grid_profit_redist_interval_sec=_read_int(
            env,
            "GRID_PROFIT_REDIST_INTERVAL_SEC",
            DEFAULT_GRID_PROFIT_REDIST_INTERVAL_SEC,
        ),
        grid_maker_offset_pct=_read_float(
            env, "GRID_MAKER_OFFSET_PCT", DEFAULT_GRID_MAKER_OFFSET_PCT
        ),
        belief_stale_hours=_read_int(env, "BELIEF_STALE_HOURS", DEFAULT_BELIEF_STALE_HOURS),
        belief_consensus_threshold=_read_int(
            env, "BELIEF_CONSENSUS_THRESHOLD", DEFAULT_BELIEF_CONSENSUS_THRESHOLD
        ),
        reentry_cooldown_hours=_read_int(
            env, "REENTRY_COOLDOWN_HOURS", DEFAULT_REENTRY_COOLDOWN_HOURS
        ),
        local_state_dir=_read_path(env, "LOCAL_STATE_DIR", DEFAULT_LOCAL_STATE_DIR),
        web_port=_read_int(env, "WEB_PORT", DEFAULT_WEB_PORT),
        telegram_bot_token=_read_optional(env, "TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_read_optional(env, "TELEGRAM_CHAT_ID"),
        circuit_breaker_threshold=_read_int(
            env, "CIRCUIT_BREAKER_THRESHOLD", DEFAULT_CIRCUIT_BREAKER_THRESHOLD
        ),
        circuit_breaker_window_sec=_read_int(
            env, "CIRCUIT_BREAKER_WINDOW_SEC", DEFAULT_CIRCUIT_BREAKER_WINDOW_SEC
        ),
        circuit_breaker_cooldown_sec=_read_int(
            env, "CIRCUIT_BREAKER_COOLDOWN_SEC", DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SEC
        ),
        reconcile_interval_sec=_read_int(
            env, "RECONCILE_INTERVAL_SEC", DEFAULT_RECONCILE_INTERVAL_SEC
        ),
        stats_min_sample_size=_read_int(
            env, "STATS_MIN_SAMPLE_SIZE", DEFAULT_STATS_MIN_SAMPLE_SIZE
        ),
        stats_normality_check=_read_bool(
            env, "STATS_NORMALITY_CHECK", DEFAULT_STATS_NORMALITY_CHECK
        ),
        stats_fail_closed=_read_bool(env, "STATS_FAIL_CLOSED", DEFAULT_STATS_FAIL_CLOSED),
        read_only_exchange=_read_bool(
            env, "READ_ONLY_EXCHANGE", DEFAULT_READ_ONLY_EXCHANGE
        ),
        disable_order_mutations=_read_bool(
            env, "DISABLE_ORDER_MUTATIONS", DEFAULT_DISABLE_ORDER_MUTATIONS
        ),
        startup_reconcile_only=_read_bool(
            env, "STARTUP_RECONCILE_ONLY", DEFAULT_STARTUP_RECONCILE_ONLY
        ),
        allowed_pairs=_read_pairs(env, "ALLOWED_PAIRS"),
        scanner_pair_discovery_ttl_sec=_read_int(
            env,
            "SCANNER_PAIR_DISCOVERY_TTL_SEC",
            DEFAULT_SCANNER_PAIR_DISCOVERY_TTL_SEC,
        ),
        scanner_max_concurrency=_read_int(
            env,
            "SCANNER_MAX_CONCURRENCY",
            DEFAULT_SCANNER_MAX_CONCURRENCY,
        ),
        scanner_timeout_sec=_read_float(
            env,
            "SCANNER_TIMEOUT_SEC",
            DEFAULT_SCANNER_TIMEOUT_SEC,
        ),
        enable_conditional_tree=_read_bool(
            env,
            "ENABLE_CONDITIONAL_TREE",
            DEFAULT_ENABLE_CONDITIONAL_TREE,
        ),
    )


def _read_required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise MissingEnvironmentVariableError(name)
    return value.strip()


def _read_optional(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _read_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw_value = _read_optional(environ, name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise InvalidEnvironmentVariableError(name, raw_value, "an integer") from exc


def _read_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw_value = _read_optional(environ, name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise InvalidEnvironmentVariableError(name, raw_value, "a float") from exc


def _read_bool(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw_value = _read_optional(environ, name)
    if raw_value is None:
        return default
    normalized = raw_value.lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise InvalidEnvironmentVariableError(name, raw_value, "a boolean")


def _read_choice(
    environ: Mapping[str, str], name: str, default: str, choices: frozenset[str]
) -> str:
    raw_value = _read_optional(environ, name)
    if raw_value is None:
        return default
    normalized = raw_value.lower()
    if normalized not in choices:
        expected = "one of: " + ", ".join(sorted(choices))
        raise InvalidEnvironmentVariableError(name, raw_value, expected)
    return normalized


def _read_pairs(environ: Mapping[str, str], name: str) -> frozenset[str]:
    raw_value = _read_optional(environ, name)
    if raw_value is None:
        return frozenset()
    pairs = [normalize_pair(p.strip()) for p in raw_value.split(",") if p.strip()]
    return frozenset(pairs)


def _read_path(environ: Mapping[str, str], name: str, default: Path) -> Path:
    raw_value = _read_optional(environ, name)
    if raw_value is None:
        return default
    return Path(raw_value)


__all__ = ["REQUIRED_ENV_VARS", "Settings", "load_settings"]
