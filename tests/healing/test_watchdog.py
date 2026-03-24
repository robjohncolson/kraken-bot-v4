from __future__ import annotations

import json
from datetime import datetime, timezone

from healing.analyzer import WatchdogAdvice
from healing.heartbeat import HeartbeatSnapshot, HeartbeatStatus, write_heartbeat
from healing.incidents import (
    AllowedAction,
    IncidentRegistry,
    IncidentSeverity,
)
from healing.ollama_analyzer import AuditRecord
from healing.watchdog import Watchdog
import healing.watchdog as watchdog_module


def _snapshot(
    *,
    timestamp: datetime | None = None,
    bot_status: HeartbeatStatus = HeartbeatStatus.HEALTHY,
    websocket_connected: bool = True,
    supabase_connected: bool = True,
) -> HeartbeatSnapshot:
    return HeartbeatSnapshot(
        timestamp=timestamp or datetime.now(timezone.utc),
        bot_status=bot_status,
        active_positions_count=2,
        open_orders_count=5,
        last_reconciliation_age_sec=12.5,
        last_belief_age_sec=45.0,
        websocket_connected=websocket_connected,
        supabase_connected=supabase_connected,
    )


def _watchdog(tmp_path) -> tuple[Watchdog, IncidentRegistry, object, object]:
    heartbeat_path = tmp_path / "state" / "bot-heartbeat.json"
    incidents_dir = tmp_path / "state" / "incidents"
    audit_path = tmp_path / "state" / "watchdog-audit.ndjson"
    registry = IncidentRegistry(incidents_dir)
    watchdog = Watchdog(
        heartbeat_path=heartbeat_path,
        incident_registry=registry,
        audit_path=audit_path,
        log_path=tmp_path / "state" / "bot.log",
    )
    return watchdog, registry, heartbeat_path, audit_path


def test_check_returns_noop_for_healthy_state(tmp_path) -> None:
    watchdog, registry, heartbeat_path, _audit_path = _watchdog(tmp_path)
    write_heartbeat(_snapshot(), heartbeat_path)

    result = watchdog.check()

    assert result.heartbeat_status is HeartbeatStatus.HEALTHY
    assert result.analyzer_used == "deterministic"
    assert result.advice.recommended_action is AllowedAction.NOOP
    assert result.new_incident is None
    assert registry.get_recent(10) == []


def test_check_records_incident_for_degraded_state(tmp_path) -> None:
    watchdog, registry, heartbeat_path, _audit_path = _watchdog(tmp_path)
    write_heartbeat(_snapshot(bot_status=HeartbeatStatus.DEGRADED), heartbeat_path)

    result = watchdog.check()

    assert result.heartbeat_status is HeartbeatStatus.DEGRADED
    assert result.analyzer_used == "deterministic"
    assert result.advice.recommended_action is AllowedAction.ESCALATE
    assert result.new_incident is not None
    assert result.new_incident.severity is IncidentSeverity.HIGH
    assert result.new_incident.recommended_action is AllowedAction.ESCALATE
    assert registry.get_recent(1) == [result.new_incident]


def test_check_uses_ollama_when_configured_and_reachable(tmp_path, monkeypatch) -> None:
    class FakeOllamaAnalyzer:
        def __init__(self, *, base_url: str, timeout_seconds: float) -> None:
            self.base_url = base_url
            self.timeout_seconds = timeout_seconds
            self.last_audit_record: AuditRecord | None = None

        def analyze(self, heartbeat, recent_incidents, recent_log_lines) -> WatchdogAdvice:
            del heartbeat, recent_incidents, recent_log_lines
            self.last_audit_record = AuditRecord(
                prompt_hash="a" * 64,
                model_name="fake-ollama-model",
                timestamp=datetime(2026, 3, 24, 20, 5, tzinfo=timezone.utc),
                raw_json_output='{"response":"retry"}',
                validation_passed=True,
                final_action=AllowedAction.RETRY,
            )
            return WatchdogAdvice(
                recommended_action=AllowedAction.RETRY,
                confidence=0.7,
                reasoning="Retry the websocket feed.",
                raw_output='{"response":"retry"}',
            )

    watchdog, registry, heartbeat_path, audit_path = _watchdog(tmp_path)
    write_heartbeat(_snapshot(), heartbeat_path)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setattr(watchdog_module, "_ollama_is_reachable", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(watchdog_module, "OllamaAnalyzer", FakeOllamaAnalyzer)

    result = watchdog.check()

    assert result.analyzer_used == "ollama"
    assert result.advice.recommended_action is AllowedAction.RETRY
    assert result.new_incident is not None
    assert registry.get_recent(1) == [result.new_incident]

    audit_record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert audit_record["model"] == "fake-ollama-model"
    assert audit_record["final_decision"] == "retry"
    assert audit_record["validation_passed"] is True


def test_check_falls_back_when_ollama_is_unreachable(tmp_path, monkeypatch) -> None:
    watchdog, _registry, heartbeat_path, _audit_path = _watchdog(tmp_path)
    write_heartbeat(_snapshot(), heartbeat_path)
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    monkeypatch.setattr(watchdog_module, "_ollama_is_reachable", lambda *_args, **_kwargs: False)

    result = watchdog.check()

    assert result.analyzer_used == "deterministic"
    assert result.advice.recommended_action is AllowedAction.NOOP
    assert result.new_incident is None


def test_check_appends_audit_trail_for_every_run(tmp_path) -> None:
    watchdog, _registry, heartbeat_path, audit_path = _watchdog(tmp_path)
    write_heartbeat(_snapshot(), heartbeat_path)

    first = watchdog.check()
    second = watchdog.check()

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first_record = json.loads(lines[0])
    second_record = json.loads(lines[1])
    assert first_record["model"] == "deterministic"
    assert first_record["raw_output"] == first.advice.raw_output
    assert first_record["validation"]["passed"] is True
    assert first_record["final_decision"] == "noop"
    assert len(first_record["prompt_hash"]) == 64
    assert second_record["raw_output"] == second.advice.raw_output
