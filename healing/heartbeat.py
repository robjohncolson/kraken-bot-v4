from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from core.errors import KrakenBotError

DEFAULT_HEARTBEAT_PATH = Path("state") / "bot-heartbeat.json"
DEFAULT_STALE_AFTER_SECONDS = 120.0


class HeartbeatError(KrakenBotError):
    """Base exception for heartbeat persistence."""


class InvalidHeartbeatError(HeartbeatError):
    """Raised when a heartbeat payload is structurally invalid."""


class InvalidHeartbeatStalenessError(HeartbeatError):
    """Raised when the configured staleness window is not positive."""

    def __init__(self, stale_after_seconds: object) -> None:
        self.stale_after_seconds = stale_after_seconds
        super().__init__(
            f"Heartbeat staleness window must be a positive number; got {stale_after_seconds!r}."
        )


class HeartbeatIOError(HeartbeatError):
    """Raised when the heartbeat file cannot be read or written."""


class HeartbeatStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class HeartbeatSnapshot:
    timestamp: datetime
    bot_status: HeartbeatStatus
    active_positions_count: int
    open_orders_count: int
    last_reconciliation_age_sec: float
    last_belief_age_sec: float
    websocket_connected: bool
    persistence_connected: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))
        object.__setattr__(self, "bot_status", _coerce_status(self.bot_status))
        object.__setattr__(
            self,
            "active_positions_count",
            _coerce_non_negative_int(
                self.active_positions_count,
                field_name="active_positions_count",
            ),
        )
        object.__setattr__(
            self,
            "open_orders_count",
            _coerce_non_negative_int(
                self.open_orders_count,
                field_name="open_orders_count",
            ),
        )
        object.__setattr__(
            self,
            "last_reconciliation_age_sec",
            _coerce_non_negative_float(
                self.last_reconciliation_age_sec,
                field_name="last_reconciliation_age_sec",
            ),
        )
        object.__setattr__(
            self,
            "last_belief_age_sec",
            _coerce_non_negative_float(
                self.last_belief_age_sec,
                field_name="last_belief_age_sec",
            ),
        )
        object.__setattr__(
            self,
            "websocket_connected",
            _coerce_bool(self.websocket_connected, field_name="websocket_connected"),
        )
        object.__setattr__(
            self,
            "persistence_connected",
            _coerce_bool(self.persistence_connected, field_name="persistence_connected"),
        )

    def to_record(self) -> dict[str, object]:
        return {
            "timestamp": _serialize_timestamp(self.timestamp),
            "bot_status": self.bot_status.value,
            "active_positions_count": self.active_positions_count,
            "open_orders_count": self.open_orders_count,
            "last_reconciliation_age_sec": self.last_reconciliation_age_sec,
            "last_belief_age_sec": self.last_belief_age_sec,
            "websocket_connected": self.websocket_connected,
            "persistence_connected": self.persistence_connected,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> HeartbeatSnapshot:
        return cls(
            timestamp=_read_timestamp(record, "timestamp"),
            bot_status=_read_string(record, "bot_status"),
            active_positions_count=_read_int(record, "active_positions_count"),
            open_orders_count=_read_int(record, "open_orders_count"),
            last_reconciliation_age_sec=_read_float(record, "last_reconciliation_age_sec"),
            last_belief_age_sec=_read_float(record, "last_belief_age_sec"),
            websocket_connected=_read_bool(record, "websocket_connected"),
            persistence_connected=_read_bool(record, "persistence_connected"),
        )


def write_heartbeat(
    snapshot: HeartbeatSnapshot,
    path: Path = DEFAULT_HEARTBEAT_PATH,
) -> None:
    payload = json.dumps(snapshot.to_record(), indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        temporary_path.write_text(payload, encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError as exc:
        raise HeartbeatIOError(f"Failed to write heartbeat to {path}.") from exc


def read_heartbeat(
    path: Path = DEFAULT_HEARTBEAT_PATH,
    *,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> HeartbeatSnapshot | None:
    stale_after = _validate_stale_after_seconds(stale_after_seconds)

    try:
        raw_payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise HeartbeatIOError(f"Failed to read heartbeat from {path}.") from exc

    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None

    if not isinstance(decoded, Mapping):
        return None

    try:
        snapshot = HeartbeatSnapshot.from_record(decoded)
    except InvalidHeartbeatError:
        return None

    reference_time = _normalize_timestamp(datetime.now(timezone.utc) if now is None else now)
    if (reference_time - snapshot.timestamp).total_seconds() >= stale_after:
        return None

    return snapshot


def _validate_stale_after_seconds(stale_after_seconds: float) -> float:
    if isinstance(stale_after_seconds, bool) or not isinstance(stale_after_seconds, (int, float)):
        raise InvalidHeartbeatStalenessError(stale_after_seconds)
    if stale_after_seconds <= 0:
        raise InvalidHeartbeatStalenessError(stale_after_seconds)
    return float(stale_after_seconds)


def _normalize_timestamp(timestamp: datetime) -> datetime:
    if not isinstance(timestamp, datetime):
        raise InvalidHeartbeatError("Heartbeat timestamp must be a datetime.")
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _serialize_timestamp(timestamp: datetime) -> str:
    return _normalize_timestamp(timestamp).isoformat().replace("+00:00", "Z")


def _parse_timestamp(raw_value: str) -> datetime:
    try:
        return _normalize_timestamp(datetime.fromisoformat(raw_value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise InvalidHeartbeatError(f"Heartbeat timestamp {raw_value!r} is not valid ISO-8601.") from exc


def _coerce_status(raw_value: HeartbeatStatus | str) -> HeartbeatStatus:
    if isinstance(raw_value, HeartbeatStatus):
        return raw_value
    try:
        return HeartbeatStatus(str(raw_value))
    except ValueError as exc:
        raise InvalidHeartbeatError(f"Unsupported heartbeat bot_status {raw_value!r}.") from exc


def _coerce_non_negative_int(raw_value: object, *, field_name: str) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise InvalidHeartbeatError(f"{field_name} must be a non-negative integer.")
    if raw_value < 0:
        raise InvalidHeartbeatError(f"{field_name} cannot be negative.")
    return raw_value


def _coerce_non_negative_float(raw_value: object, *, field_name: str) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise InvalidHeartbeatError(f"{field_name} must be a non-negative number.")
    numeric_value = float(raw_value)
    if numeric_value < 0:
        raise InvalidHeartbeatError(f"{field_name} cannot be negative.")
    return numeric_value


def _coerce_bool(raw_value: object, *, field_name: str) -> bool:
    if not isinstance(raw_value, bool):
        raise InvalidHeartbeatError(f"{field_name} must be a boolean.")
    return raw_value


def _read_timestamp(record: Mapping[str, object], field_name: str) -> datetime:
    return _parse_timestamp(_read_string(record, field_name))


def _read_string(record: Mapping[str, object], field_name: str) -> str:
    raw_value = record.get(field_name)
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise InvalidHeartbeatError(f"{field_name} must be a non-empty string.")
    return raw_value


def _read_int(record: Mapping[str, object], field_name: str) -> int:
    return _coerce_non_negative_int(record.get(field_name), field_name=field_name)


def _read_float(record: Mapping[str, object], field_name: str) -> float:
    return _coerce_non_negative_float(record.get(field_name), field_name=field_name)


def _read_bool(record: Mapping[str, object], field_name: str) -> bool:
    return _coerce_bool(record.get(field_name), field_name=field_name)


__all__ = [
    "DEFAULT_HEARTBEAT_PATH",
    "DEFAULT_STALE_AFTER_SECONDS",
    "HeartbeatError",
    "HeartbeatIOError",
    "HeartbeatSnapshot",
    "HeartbeatStatus",
    "InvalidHeartbeatError",
    "InvalidHeartbeatStalenessError",
    "read_heartbeat",
    "write_heartbeat",
]
