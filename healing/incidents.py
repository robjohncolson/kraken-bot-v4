from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Never, cast
from uuid import UUID

from core.errors import KrakenBotError

DEFAULT_INCIDENTS_DIR = Path("state") / "incidents"


class IncidentError(KrakenBotError):
    """Base exception for incident persistence."""


class InvalidIncidentError(IncidentError):
    """Raised when an incident payload is structurally invalid."""


class IncidentIOError(IncidentError):
    """Raised when an incident record cannot be read or written."""


class DuplicateIncidentError(IncidentError):
    """Raised when an incident is recorded more than once."""

    def __init__(self, incident_id: UUID) -> None:
        self.incident_id = incident_id
        super().__init__(f"Incident {incident_id} is already recorded.")


class IncidentNotFoundError(IncidentError):
    """Raised when a registry operation targets a missing incident."""

    def __init__(self, incident_id: UUID) -> None:
        self.incident_id = incident_id
        super().__init__(f"Incident {incident_id} was not found.")


class IncidentSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IncidentCategory(StrEnum):
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    ORDER_FAILURE = "order_failure"
    WEBSOCKET_DROP = "websocket_drop"
    BELIEF_STALE = "belief_stale"
    PERSISTENCE_UNAVAILABLE = "persistence_unavailable"
    UNKNOWN = "unknown"


class AllowedAction(StrEnum):
    NOOP = "noop"
    RETRY = "retry"
    PAUSE_COMPONENT = "pause_component"
    RUN_RECONCILE = "run_reconcile"
    ESCALATE = "escalate"
    RESTART_NONTRADING_COMPONENT = "restart_nontrading_component"


class _FrozenJSONDict(dict[str, object]):
    def __init__(self, values: Mapping[str, object]) -> None:
        super().__init__()
        for key, value in values.items():
            dict.__setitem__(self, key, _freeze_json_value(value))

    def _immutable(self, *_args: object, **_kwargs: object) -> Never:
        raise TypeError("Incident context is immutable.")

    __delitem__ = __setitem__ = clear = pop = popitem = setdefault = update = _immutable


class _FrozenJSONList(list[object]):
    def __init__(self, values: list[object]) -> None:
        super().__init__(_freeze_json_value(value) for value in values)

    def _immutable(self, *_args: object, **_kwargs: object) -> Never:
        raise TypeError("Incident context is immutable.")

    __delitem__ = __setitem__ = append = clear = extend = insert = pop = remove = reverse = sort = _immutable
    __iadd__ = __imul__ = _immutable


@dataclass(frozen=True, slots=True)
class Incident:
    incident_id: UUID
    timestamp: datetime
    severity: IncidentSeverity
    category: IncidentCategory
    description: str
    context: dict[str, object]
    recommended_action: AllowedAction

    def __post_init__(self) -> None:
        object.__setattr__(self, "incident_id", _coerce_uuid(self.incident_id, field_name="incident_id"))
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))
        object.__setattr__(self, "severity", _coerce_severity(self.severity))
        object.__setattr__(self, "category", _coerce_category(self.category))
        object.__setattr__(
            self,
            "description",
            _coerce_non_empty_string(self.description, field_name="description"),
        )
        object.__setattr__(self, "context", _coerce_context(self.context))
        object.__setattr__(self, "recommended_action", _coerce_action(self.recommended_action))

    def to_record(self) -> dict[str, object]:
        return {
            "record_type": "incident",
            "incident_id": str(self.incident_id),
            "timestamp": _serialize_timestamp(self.timestamp),
            "severity": self.severity.value,
            "category": self.category.value,
            "description": self.description,
            "context": _clone_context(self.context),
            "recommended_action": self.recommended_action.value,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> Incident:
        record_type = record.get("record_type")
        if record_type not in (None, "incident"):
            raise InvalidIncidentError(f"Unsupported incident record_type {record_type!r}.")
        return cls(
            incident_id=_coerce_uuid(record.get("incident_id"), field_name="incident_id"),
            timestamp=_read_timestamp(record, "timestamp"),
            severity=_coerce_severity(_read_string(record, "severity")),
            category=_coerce_category(_read_string(record, "category")),
            description=_read_string(record, "description"),
            context=_read_context(record, "context"),
            recommended_action=_coerce_action(_read_string(record, "recommended_action")),
        )


@dataclass(frozen=True, slots=True)
class _ResolutionRecord:
    incident_id: UUID
    timestamp: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "incident_id", _coerce_uuid(self.incident_id, field_name="incident_id"))
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))

    def to_record(self) -> dict[str, object]:
        return {
            "record_type": "resolution",
            "incident_id": str(self.incident_id),
            "timestamp": _serialize_timestamp(self.timestamp),
            "status": "resolved",
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> _ResolutionRecord:
        if record.get("record_type") != "resolution":
            raise InvalidIncidentError("Resolution records must declare record_type='resolution'.")
        if _read_string(record, "status") != "resolved":
            raise InvalidIncidentError("Resolution records must declare status='resolved'.")
        return cls(
            incident_id=_coerce_uuid(record.get("incident_id"), field_name="incident_id"),
            timestamp=_read_timestamp(record, "timestamp"),
        )


class IncidentRegistry:
    def __init__(self, directory: Path = DEFAULT_INCIDENTS_DIR) -> None:
        self._directory = directory

    def record_incident(self, incident: Incident) -> Path:
        incidents, _ = self._load_state()
        if any(existing.incident_id == incident.incident_id for existing in incidents):
            raise DuplicateIncidentError(incident.incident_id)

        path = self._directory / _build_record_filename(
            timestamp=incident.timestamp,
            incident_id=incident.incident_id,
            record_type="incident",
        )
        _write_record(path, incident.to_record())
        return path

    def get_recent(
        self,
        n: int,
        *,
        severity: IncidentSeverity | str | None = None,
    ) -> list[Incident]:
        limit = _coerce_limit(n)
        if limit == 0:
            return []

        incidents, _ = self._load_state()
        filtered = self._filter_by_severity(incidents, severity=severity)
        return _sort_incidents(filtered)[:limit]

    def get_unresolved(
        self,
        *,
        severity: IncidentSeverity | str | None = None,
    ) -> list[Incident]:
        incidents, resolved_ids = self._load_state()
        unresolved = [incident for incident in incidents if incident.incident_id not in resolved_ids]
        return _sort_incidents(self._filter_by_severity(unresolved, severity=severity))

    def resolve_incident(
        self,
        incident_id: UUID | str,
        *,
        timestamp: datetime | None = None,
    ) -> bool:
        normalized_incident_id = _coerce_uuid(incident_id, field_name="incident_id")
        incidents, resolved_ids = self._load_state()
        if normalized_incident_id not in {incident.incident_id for incident in incidents}:
            raise IncidentNotFoundError(normalized_incident_id)
        if normalized_incident_id in resolved_ids:
            return False

        resolution = _ResolutionRecord(
            incident_id=normalized_incident_id,
            timestamp=datetime.now(timezone.utc) if timestamp is None else timestamp,
        )
        path = self._directory / _build_record_filename(
            timestamp=resolution.timestamp,
            incident_id=normalized_incident_id,
            record_type="resolution",
        )
        _write_record(path, resolution.to_record())
        return True

    def _filter_by_severity(
        self,
        incidents: list[Incident],
        *,
        severity: IncidentSeverity | str | None,
    ) -> list[Incident]:
        if severity is None:
            return incidents
        normalized_severity = _coerce_severity(severity)
        return [incident for incident in incidents if incident.severity == normalized_severity]

    def _load_state(self) -> tuple[list[Incident], set[UUID]]:
        if not self._directory.exists():
            return [], set()

        incidents: list[Incident] = []
        resolved_ids: set[UUID] = set()
        for path in sorted(self._directory.glob("*.json")):
            record = _read_record(path)
            record_type = _read_string(record, "record_type")
            if record_type == "incident":
                incidents.append(Incident.from_record(record))
                continue
            if record_type == "resolution":
                resolved_ids.add(_ResolutionRecord.from_record(record).incident_id)
                continue
            raise InvalidIncidentError(f"Unsupported incident record_type {record_type!r} in {path}.")

        return incidents, resolved_ids


def _sort_incidents(incidents: list[Incident]) -> list[Incident]:
    return sorted(
        incidents,
        key=lambda incident: (incident.timestamp, str(incident.incident_id)),
        reverse=True,
    )


def _write_record(path: Path, record: Mapping[str, object]) -> None:
    payload = json.dumps(record, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    try:
        temporary_path.write_text(payload, encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError as exc:
        raise IncidentIOError(f"Failed to write incident record to {path}.") from exc


def _read_record(path: Path) -> Mapping[str, object]:
    try:
        raw_payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise IncidentIOError(f"Failed to read incident record from {path}.") from exc

    try:
        decoded = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise InvalidIncidentError(f"Incident record {path} contains invalid JSON.") from exc

    if not isinstance(decoded, Mapping):
        raise InvalidIncidentError(f"Incident record {path} must decode to an object.")
    return decoded


def _build_record_filename(*, timestamp: datetime, incident_id: UUID, record_type: str) -> str:
    stamp = _normalize_timestamp(timestamp).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}_{incident_id}_{record_type}.json"


def _coerce_limit(raw_value: object) -> int:
    if isinstance(raw_value, bool) or not isinstance(raw_value, int):
        raise InvalidIncidentError("Incident query limit must be a non-negative integer.")
    if raw_value < 0:
        raise InvalidIncidentError("Incident query limit cannot be negative.")
    return raw_value


def _normalize_timestamp(timestamp: datetime) -> datetime:
    if not isinstance(timestamp, datetime):
        raise InvalidIncidentError("Incident timestamp must be a datetime.")
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _serialize_timestamp(timestamp: datetime) -> str:
    return _normalize_timestamp(timestamp).isoformat().replace("+00:00", "Z")


def _read_timestamp(record: Mapping[str, object], field_name: str) -> datetime:
    raw_value = _read_string(record, field_name)
    try:
        return _normalize_timestamp(datetime.fromisoformat(raw_value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise InvalidIncidentError(f"{field_name} {raw_value!r} is not valid ISO-8601.") from exc


def _coerce_uuid(raw_value: object, *, field_name: str) -> UUID:
    if isinstance(raw_value, UUID):
        return raw_value
    try:
        return UUID(_coerce_non_empty_string(raw_value, field_name=field_name))
    except ValueError as exc:
        raise InvalidIncidentError(f"{field_name} must be a valid UUID.") from exc


def _coerce_severity(raw_value: IncidentSeverity | str) -> IncidentSeverity:
    if isinstance(raw_value, IncidentSeverity):
        return raw_value
    try:
        return IncidentSeverity(_coerce_non_empty_string(raw_value, field_name="severity"))
    except ValueError as exc:
        raise InvalidIncidentError(f"Unsupported incident severity {raw_value!r}.") from exc


def _coerce_category(raw_value: IncidentCategory | str) -> IncidentCategory:
    if isinstance(raw_value, IncidentCategory):
        return raw_value
    try:
        return IncidentCategory(_coerce_non_empty_string(raw_value, field_name="category"))
    except ValueError as exc:
        raise InvalidIncidentError(f"Unsupported incident category {raw_value!r}.") from exc


def _coerce_action(raw_value: AllowedAction | str) -> AllowedAction:
    if isinstance(raw_value, AllowedAction):
        return raw_value
    try:
        return AllowedAction(_coerce_non_empty_string(raw_value, field_name="recommended_action"))
    except ValueError as exc:
        raise InvalidIncidentError(f"Unsupported recommended_action {raw_value!r}.") from exc


def _coerce_non_empty_string(raw_value: object, *, field_name: str) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise InvalidIncidentError(f"{field_name} must be a non-empty string.")
    return raw_value


def _read_string(record: Mapping[str, object], field_name: str) -> str:
    return _coerce_non_empty_string(record.get(field_name), field_name=field_name)


def _read_context(record: Mapping[str, object], field_name: str) -> dict[str, object]:
    return _coerce_context(record.get(field_name))


def _coerce_context(raw_value: object) -> dict[str, object]:
    if not isinstance(raw_value, Mapping):
        raise InvalidIncidentError("context must be a JSON object.")
    cloned = _round_trip_json(
        dict(raw_value),
        error_type=InvalidIncidentError,
        error_message="context must be JSON-serializable.",
    )
    if not isinstance(cloned, dict):
        raise InvalidIncidentError("context must be a JSON object.")
    return _FrozenJSONDict(cloned)


def _clone_context(context: dict[str, object]) -> dict[str, object]:
    cloned = _round_trip_json(
        dict(context),
        error_type=IncidentIOError,
        error_message="Failed to serialize incident context.",
    )
    if not isinstance(cloned, dict):
        raise IncidentIOError("Failed to serialize incident context.")
    return cast(dict[str, object], cloned)


def _freeze_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _FrozenJSONDict(cast(Mapping[str, object], value))
    if isinstance(value, list):
        return _FrozenJSONList(value)
    return value


def _round_trip_json(
    value: object,
    *,
    error_type: type[IncidentError],
    error_message: str,
) -> object:
    try:
        return json.loads(json.dumps(value, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise error_type(error_message) from exc


__all__ = [
    "AllowedAction",
    "DEFAULT_INCIDENTS_DIR",
    "DuplicateIncidentError",
    "Incident",
    "IncidentCategory",
    "IncidentError",
    "IncidentIOError",
    "IncidentNotFoundError",
    "IncidentRegistry",
    "IncidentSeverity",
    "InvalidIncidentError",
]
