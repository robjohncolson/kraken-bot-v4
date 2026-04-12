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
DEFAULT_KELLY_MIN_SAMPLE_SIZE = 10
DEFAULT_MIN_POSITION_USD = 10
DEFAULT_MAX_POSITION_USD = 100
DEFAULT_STOP_PCT = 5
DEFAULT_TARGET_PCT = 10
DEFAULT_GRID_HEADROOM_PCT = 70
DEFAULT_GRID_PROFIT_REDIST_INTERVAL_SEC = 3600
DEFAULT_GRID_MAKER_OFFSET_PCT = 0.4
DEFAULT_BELIEF_STALE_HOURS = 4
DEFAULT_BELIEF_CONSENSUS_THRESHOLD = 2
DEFAULT_MIN_BELIEF_CONFIDENCE = 0.5
DEFAULT_ROTATION_TAKE_PROFIT_PCT = 5.0
DEFAULT_ROTATION_STOP_LOSS_PCT = 2.5
DEFAULT_ROTATION_TRAILING_STOP_ACTIVATION_PCT = 1.5
DEFAULT_ROTATION_ENTRY_FILL_TIMEOUT_MIN = 30
DEFAULT_ROTATION_EXIT_FILL_TIMEOUT_MIN = 5
DEFAULT_KRAKEN_MAKER_FEE_PCT = 0.26
DEFAULT_KRAKEN_TAKER_FEE_PCT = 0.40
DEFAULT_ROOT_STOP_LOSS_PCT = 10.0
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
DEFAULT_SCANNER_MIN_24H_VOLUME_USD = 50_000.0
DEFAULT_SCANNER_MAX_SPREAD_PCT = 2.0
DEFAULT_ENABLE_CONDITIONAL_TREE = False
DEFAULT_ENABLE_ROTATION_TREE = False
DEFAULT_CC_BRAIN_MODE = False
DEFAULT_CC_ORDER_MAX_AGE_MINUTES = 15
DEFAULT_EXIT_LIMIT_OFFSET_PCT = 0.1
DEFAULT_ROTATION_MAX_CHILDREN_PER_PARENT = 3
DEFAULT_ROTATION_MIN_CONFIDENCE = 0.65
DEFAULT_PAIR_METADATA_REFRESH_HOURS = 24
DEFAULT_MTF_4H_GATE_ENABLED = False
DEFAULT_MTF_ALIGNED_BOOST = 1.15
DEFAULT_MTF_COUNTER_PENALTY = 0.3
DEFAULT_MTF_15M_CONFIRM_ENABLED = False
DEFAULT_MTF_15M_MAX_DEFERRALS = 6

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
    kelly_min_sample_size: int
    min_position_usd: int
    max_position_usd: int
    default_stop_pct: int
    default_target_pct: int
    grid_headroom_pct: int
    grid_profit_redist_interval_sec: int
    grid_maker_offset_pct: float
    belief_stale_hours: int
    belief_consensus_threshold: int
    min_belief_confidence: float
    rotation_take_profit_pct: float
    rotation_stop_loss_pct: float
    rotation_trailing_stop_activation_pct: float
    rotation_entry_fill_timeout_min: int
    rotation_exit_fill_timeout_min: int
    kraken_maker_fee_pct: float
    kraken_taker_fee_pct: float
    root_stop_loss_pct: float
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
    scanner_min_24h_volume_usd: float
    scanner_max_spread_pct: float
    enable_conditional_tree: bool
    enable_rotation_tree: bool
    cc_brain_mode: bool
    cc_order_max_age_minutes: int
    exit_limit_offset_pct: float
    pair_metadata_refresh_hours: int
    rotation_max_children_per_parent: int
    rotation_min_confidence: float
    mtf_4h_gate_enabled: bool
    mtf_aligned_boost: float
    mtf_counter_penalty: float
    mtf_15m_confirm_enabled: bool
    mtf_15m_max_deferrals: int
    belief_model: str
    active_artifact_id: str | None
    shadow_artifact_id: str | None


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    return Settings(
        kraken_api_key=_read_required(env, "KRAKEN_API_KEY"),
        kraken_api_secret=_read_required(env, "KRAKEN_API_SECRET"),
        kraken_tier=_read_choice(
            env, "KRAKEN_TIER", DEFAULT_KRAKEN_TIER, ALLOWED_KRAKEN_TIERS
        ),
        sqlite_path=_read_path(env, "SQLITE_PATH", DEFAULT_SQLITE_PATH),
        web_host=_read_optional(env, "WEB_HOST") or DEFAULT_WEB_HOST,
        supabase_url=_read_optional(env, "SUPABASE_URL"),
        supabase_key=_read_optional(env, "SUPABASE_KEY"),
        max_positions=_read_int(env, "MAX_POSITIONS", DEFAULT_MAX_POSITIONS),
        max_same_side_pct=_read_int(
            env, "MAX_SAME_SIDE_PCT", DEFAULT_MAX_SAME_SIDE_PCT
        ),
        max_single_pair_pct=_read_int(
            env, "MAX_SINGLE_PAIR_PCT", DEFAULT_MAX_SINGLE_PAIR_PCT
        ),
        max_drawdown_soft_pct=_read_int(
            env, "MAX_DRAWDOWN_SOFT_PCT", DEFAULT_MAX_DRAWDOWN_SOFT_PCT
        ),
        max_drawdown_hard_pct=_read_int(
            env, "MAX_DRAWDOWN_HARD_PCT", DEFAULT_MAX_DRAWDOWN_HARD_PCT
        ),
        kelly_ci_level=_read_float(env, "KELLY_CI_LEVEL", DEFAULT_KELLY_CI_LEVEL),
        kelly_min_sample_size=_read_int(
            env,
            "KELLY_MIN_SAMPLE_SIZE",
            DEFAULT_KELLY_MIN_SAMPLE_SIZE,
        ),
        min_position_usd=_read_int(env, "MIN_POSITION_USD", DEFAULT_MIN_POSITION_USD),
        max_position_usd=_read_int(env, "MAX_POSITION_USD", DEFAULT_MAX_POSITION_USD),
        default_stop_pct=_read_int(env, "DEFAULT_STOP_PCT", DEFAULT_STOP_PCT),
        default_target_pct=_read_int(env, "DEFAULT_TARGET_PCT", DEFAULT_TARGET_PCT),
        grid_headroom_pct=_read_int(
            env, "GRID_HEADROOM_PCT", DEFAULT_GRID_HEADROOM_PCT
        ),
        grid_profit_redist_interval_sec=_read_int(
            env,
            "GRID_PROFIT_REDIST_INTERVAL_SEC",
            DEFAULT_GRID_PROFIT_REDIST_INTERVAL_SEC,
        ),
        grid_maker_offset_pct=_read_float(
            env, "GRID_MAKER_OFFSET_PCT", DEFAULT_GRID_MAKER_OFFSET_PCT
        ),
        belief_stale_hours=_read_int(
            env, "BELIEF_STALE_HOURS", DEFAULT_BELIEF_STALE_HOURS
        ),
        belief_consensus_threshold=_read_int(
            env, "BELIEF_CONSENSUS_THRESHOLD", DEFAULT_BELIEF_CONSENSUS_THRESHOLD
        ),
        min_belief_confidence=_read_float(
            env, "MIN_BELIEF_CONFIDENCE", DEFAULT_MIN_BELIEF_CONFIDENCE
        ),
        rotation_take_profit_pct=_read_float(
            env, "ROTATION_TAKE_PROFIT_PCT", DEFAULT_ROTATION_TAKE_PROFIT_PCT
        ),
        rotation_stop_loss_pct=_read_float(
            env, "ROTATION_STOP_LOSS_PCT", DEFAULT_ROTATION_STOP_LOSS_PCT
        ),
        rotation_trailing_stop_activation_pct=_read_float(
            env,
            "ROTATION_TRAILING_STOP_ACTIVATION_PCT",
            DEFAULT_ROTATION_TRAILING_STOP_ACTIVATION_PCT,
        ),
        rotation_entry_fill_timeout_min=_read_int(
            env,
            "ROTATION_ENTRY_FILL_TIMEOUT_MIN",
            DEFAULT_ROTATION_ENTRY_FILL_TIMEOUT_MIN,
        ),
        rotation_exit_fill_timeout_min=_read_int(
            env,
            "ROTATION_EXIT_FILL_TIMEOUT_MIN",
            DEFAULT_ROTATION_EXIT_FILL_TIMEOUT_MIN,
        ),
        kraken_maker_fee_pct=_read_float(
            env, "KRAKEN_MAKER_FEE_PCT", DEFAULT_KRAKEN_MAKER_FEE_PCT
        ),
        kraken_taker_fee_pct=_read_float(
            env, "KRAKEN_TAKER_FEE_PCT", DEFAULT_KRAKEN_TAKER_FEE_PCT
        ),
        root_stop_loss_pct=_read_float(
            env, "ROOT_STOP_LOSS_PCT", DEFAULT_ROOT_STOP_LOSS_PCT
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
        stats_fail_closed=_read_bool(
            env, "STATS_FAIL_CLOSED", DEFAULT_STATS_FAIL_CLOSED
        ),
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
        scanner_min_24h_volume_usd=_read_float(
            env,
            "SCANNER_MIN_24H_VOLUME_USD",
            DEFAULT_SCANNER_MIN_24H_VOLUME_USD,
        ),
        scanner_max_spread_pct=_read_float(
            env,
            "SCANNER_MAX_SPREAD_PCT",
            DEFAULT_SCANNER_MAX_SPREAD_PCT,
        ),
        enable_conditional_tree=_read_bool(
            env,
            "ENABLE_CONDITIONAL_TREE",
            DEFAULT_ENABLE_CONDITIONAL_TREE,
        ),
        enable_rotation_tree=_read_bool(
            env,
            "ENABLE_ROTATION_TREE",
            DEFAULT_ENABLE_ROTATION_TREE,
        ),
        cc_brain_mode=_read_bool(
            env,
            "CC_BRAIN_MODE",
            DEFAULT_CC_BRAIN_MODE,
        ),
        cc_order_max_age_minutes=_read_int(
            env,
            "CC_ORDER_MAX_AGE_MINUTES",
            DEFAULT_CC_ORDER_MAX_AGE_MINUTES,
        ),
        exit_limit_offset_pct=_read_float(
            env,
            "EXIT_LIMIT_OFFSET_PCT",
            DEFAULT_EXIT_LIMIT_OFFSET_PCT,
        ),
        pair_metadata_refresh_hours=_read_int(
            env,
            "PAIR_METADATA_REFRESH_HOURS",
            DEFAULT_PAIR_METADATA_REFRESH_HOURS,
        ),
        rotation_max_children_per_parent=_read_int(
            env,
            "ROTATION_MAX_CHILDREN_PER_PARENT",
            DEFAULT_ROTATION_MAX_CHILDREN_PER_PARENT,
        ),
        rotation_min_confidence=_read_float(
            env,
            "ROTATION_MIN_CONFIDENCE",
            DEFAULT_ROTATION_MIN_CONFIDENCE,
        ),
        mtf_4h_gate_enabled=_read_bool(
            env, "MTF_4H_GATE_ENABLED", DEFAULT_MTF_4H_GATE_ENABLED,
        ),
        mtf_aligned_boost=_read_float(
            env, "MTF_ALIGNED_BOOST", DEFAULT_MTF_ALIGNED_BOOST,
        ),
        mtf_counter_penalty=_read_float(
            env, "MTF_COUNTER_PENALTY", DEFAULT_MTF_COUNTER_PENALTY,
        ),
        mtf_15m_confirm_enabled=_read_bool(
            env, "MTF_15M_CONFIRM_ENABLED", DEFAULT_MTF_15M_CONFIRM_ENABLED,
        ),
        mtf_15m_max_deferrals=_read_int(
            env, "MTF_15M_MAX_DEFERRALS", DEFAULT_MTF_15M_MAX_DEFERRALS,
        ),
        belief_model=env.get("BELIEF_MODEL", "technical_ensemble").strip(),
        active_artifact_id=_read_optional(env, "ACTIVE_ARTIFACT_ID"),
        shadow_artifact_id=_read_optional(env, "SHADOW_ARTIFACT_ID"),
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


def validate_settings(settings: Settings) -> list[str]:
    """Validate settings and return a list of warning messages."""
    warnings: list[str] = []
    if settings.rotation_take_profit_pct <= 0:
        warnings.append("ROTATION_TAKE_PROFIT_PCT must be > 0")
    elif settings.rotation_take_profit_pct < 1.0:
        warnings.append(
            f"ROTATION_TAKE_PROFIT_PCT={settings.rotation_take_profit_pct} is very low — "
            "fees may eat the profit"
        )
    elif settings.rotation_take_profit_pct > 10.0:
        warnings.append(
            f"ROTATION_TAKE_PROFIT_PCT={settings.rotation_take_profit_pct} is high — "
            "may rarely trigger"
        )
    if settings.rotation_stop_loss_pct <= 0:
        warnings.append("ROTATION_STOP_LOSS_PCT must be > 0")
    elif settings.rotation_stop_loss_pct > 5.0:
        warnings.append(
            f"ROTATION_STOP_LOSS_PCT={settings.rotation_stop_loss_pct} is loose — "
            "large losses before exit"
        )
    if not (0.0 <= settings.min_belief_confidence <= 1.0):
        warnings.append(
            f"MIN_BELIEF_CONFIDENCE={settings.min_belief_confidence} must be in [0.0, 1.0]"
        )
    if not (0.0 <= settings.rotation_min_confidence <= 1.0):
        warnings.append(
            f"ROTATION_MIN_CONFIDENCE={settings.rotation_min_confidence} "
            "must be in [0.0, 1.0]"
        )
    if settings.rotation_entry_fill_timeout_min < 5:
        warnings.append(
            f"ROTATION_ENTRY_FILL_TIMEOUT_MIN={settings.rotation_entry_fill_timeout_min} "
            "is short — entries may get cancelled prematurely"
        )
    if settings.rotation_exit_fill_timeout_min < 1:
        warnings.append(
            f"ROTATION_EXIT_FILL_TIMEOUT_MIN={settings.rotation_exit_fill_timeout_min} "
            "is very short — exits may escalate to MARKET too quickly"
        )
    return warnings


__all__ = ["REQUIRED_ENV_VARS", "Settings", "load_settings", "validate_settings"]
