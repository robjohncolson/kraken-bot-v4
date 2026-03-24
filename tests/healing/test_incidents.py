from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from healing.incidents import (
    AllowedAction,
    Incident,
    IncidentCategory,
    IncidentIOError,
    IncidentNotFoundError,
    IncidentRegistry,
    IncidentSeverity,
    InvalidIncidentError,
)


def _incident(
    *,
    incident_id=None,
    timestamp: datetime | None = None,
    severity: IncidentSeverity | str = IncidentSeverity.MEDIUM,
    category: IncidentCategory | str = IncidentCategory.UNKNOWN,
    description: str = "Incident detected.",
    context: dict[str, object] | None = None,
    recommended_action: AllowedAction | str = AllowedAction.NOOP,
) -> Incident:
    return Incident(
        incident_id=incident_id or uuid4(),
        timestamp=timestamp or datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc),
        severity=severity,
        category=category,
        description=description,
        context=context or {"pair": "DOGE/USD"},
        recommended_action=recommended_action,
    )


def test_incident_creation_normalizes_and_round_trips() -> None:
    raw_context = {"pair": "DOGE/USD", "attempts": 2}
    incident = _incident(
        timestamp=datetime(2026, 3, 24, 20, 0),
        severity="high",
        category="order_failure",
        context=raw_context,
        recommended_action="retry",
    )

    assert incident.timestamp == datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc)
    assert incident.severity is IncidentSeverity.HIGH
    assert incident.category is IncidentCategory.ORDER_FAILURE
    assert incident.recommended_action is AllowedAction.RETRY
    assert incident.context == raw_context
    assert incident.context is not raw_context
    assert Incident.from_record(incident.to_record()) == incident


def test_incident_context_is_deeply_immutable() -> None:
    incident = _incident(context={"pair": "DOGE/USD", "meta": {"attempts": [1, 2]}})

    with pytest.raises(TypeError):
        incident.context["pair"] = "BTC/USD"  # type: ignore[index]
    with pytest.raises(TypeError):
        incident.context["meta"]["attempts"].append(3)  # type: ignore[index]


def test_incident_rejects_circular_context_with_typed_error() -> None:
    circular: dict[str, object] = {}
    circular["self"] = circular

    with pytest.raises(InvalidIncidentError):
        _incident(context=circular)


def test_registry_record_incident_and_get_recent(tmp_path) -> None:
    directory = tmp_path / "state" / "incidents"
    registry = IncidentRegistry(directory)
    older = _incident(timestamp=datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc))
    newer = _incident(timestamp=datetime(2026, 3, 24, 20, 5, tzinfo=timezone.utc))

    registry.record_incident(older)
    registry.record_incident(newer)

    recent = registry.get_recent(1)

    assert recent == [newer]
    files = sorted(directory.glob("*.json"))
    assert len(files) == 2
    assert json.loads(files[0].read_text(encoding="utf-8"))["record_type"] == "incident"


def test_registry_filters_by_severity(tmp_path) -> None:
    registry = IncidentRegistry(tmp_path / "state" / "incidents")
    low = _incident(severity=IncidentSeverity.LOW)
    critical = _incident(
        timestamp=low.timestamp + timedelta(minutes=1),
        severity=IncidentSeverity.CRITICAL,
        category=IncidentCategory.RECONCILIATION_MISMATCH,
        recommended_action=AllowedAction.ESCALATE,
    )

    registry.record_incident(low)
    registry.record_incident(critical)

    recent = registry.get_recent(10, severity="critical")
    unresolved = registry.get_unresolved(severity=IncidentSeverity.CRITICAL)

    assert recent == [critical]
    assert unresolved == [critical]


def test_registry_resolution_marks_incident_resolved_append_only(tmp_path) -> None:
    directory = tmp_path / "state" / "incidents"
    registry = IncidentRegistry(directory)
    first = _incident(severity=IncidentSeverity.HIGH)
    second = _incident(
        timestamp=first.timestamp + timedelta(minutes=1),
        severity=IncidentSeverity.CRITICAL,
    )

    registry.record_incident(first)
    registry.record_incident(second)

    assert registry.get_unresolved() == [second, first]
    assert registry.resolve_incident(first.incident_id, timestamp=second.timestamp + timedelta(minutes=1))
    assert registry.get_unresolved() == [second]
    assert not registry.resolve_incident(first.incident_id, timestamp=second.timestamp + timedelta(minutes=2))

    files = sorted(directory.glob("*.json"))
    assert len(files) == 3
    records = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    assert any(
        record["record_type"] == "resolution" and record["incident_id"] == str(first.incident_id)
        for record in records
    )


def test_registry_resolution_raises_for_missing_incident(tmp_path) -> None:
    registry = IncidentRegistry(tmp_path / "state" / "incidents")

    with pytest.raises(IncidentNotFoundError):
        registry.resolve_incident(uuid4())


def test_record_incident_wraps_context_serialization_errors(tmp_path) -> None:
    registry = IncidentRegistry(tmp_path / "state" / "incidents")
    incident = _incident()
    circular: dict[str, object] = {}
    circular["self"] = circular

    object.__setattr__(incident, "context", circular)

    with pytest.raises(IncidentIOError):
        registry.record_incident(incident)
