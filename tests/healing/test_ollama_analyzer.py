from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from healing.analyzer import WatchdogAnalyzer
from healing.heartbeat import HeartbeatSnapshot, HeartbeatStatus
from healing.incidents import AllowedAction, Incident, IncidentCategory, IncidentSeverity
from healing.ollama_analyzer import (
    DEFAULT_TIMEOUT_SECONDS,
    OllamaAnalyzer,
    WATCHDOG_ADVICE_SCHEMA,
)


class _MockHTTPResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> _MockHTTPResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        del exc_type, exc, tb
        return False

    def read(self) -> bytes:
        return self._body


def _snapshot() -> HeartbeatSnapshot:
    return HeartbeatSnapshot(
        timestamp=datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc),
        bot_status=HeartbeatStatus.HEALTHY,
        active_positions_count=2,
        open_orders_count=5,
        last_reconciliation_age_sec=12.5,
        last_belief_age_sec=45.0,
        websocket_connected=True,
        persistence_connected=True,
    )


def _incident(
    *,
    category: IncidentCategory = IncidentCategory.WEBSOCKET_DROP,
    severity: IncidentSeverity = IncidentSeverity.MEDIUM,
    recommended_action: AllowedAction = AllowedAction.RETRY,
) -> Incident:
    return Incident(
        incident_id=uuid4(),
        timestamp=datetime(2026, 3, 24, 20, 1, tzinfo=timezone.utc),
        severity=severity,
        category=category,
        description=f"{category.value} detected.",
        context={"pair": "DOGE/USD"},
        recommended_action=recommended_action,
    )


def _outer_response(structured: dict[str, object]) -> str:
    return json.dumps(
        {
            "model": "llama3.1:8b",
            "done": True,
            "response": json.dumps(structured, sort_keys=True),
        },
        sort_keys=True,
    )


def test_ollama_analyzer_satisfies_protocol_contract() -> None:
    analyzer = OllamaAnalyzer()

    assert isinstance(analyzer, WatchdogAnalyzer)


def test_analyze_returns_structured_response_and_records_audit(monkeypatch) -> None:
    captured: dict[str, object] = {}
    log_lines = [f"line {index}" for index in range(25)]
    incidents = [_incident(), _incident(category=IncidentCategory.ORDER_FAILURE)]

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _MockHTTPResponse(
            _outer_response(
                {
                    "recommended_action": "retry",
                    "confidence": 0.72,
                    "reasoning": "Retry the websocket feed.",
                    "raw_output": "websocket disconnected in recent logs",
                }
            )
        )

    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:11434")
    with patch("healing.ollama_analyzer.urlopen", side_effect=fake_urlopen):
        analyzer = OllamaAnalyzer()
        advice = analyzer.analyze(_snapshot(), incidents, log_lines)

    assert advice.recommended_action is AllowedAction.RETRY
    assert advice.confidence == 0.72
    assert advice.reasoning == "Retry the websocket feed."
    assert json.loads(advice.raw_output)["done"] is True
    assert captured["url"] == "http://127.0.0.1:11434/api/generate"
    assert captured["timeout"] == DEFAULT_TIMEOUT_SECONDS

    payload = captured["payload"]
    assert payload["stream"] is False
    assert payload["format"] == WATCHDOG_ADVICE_SCHEMA
    prompt_payload = json.loads(payload["prompt"])
    assert prompt_payload["allowed_actions"] == [action.value for action in AllowedAction]
    assert prompt_payload["heartbeat_snapshot"]["bot_status"] == "healthy"
    assert prompt_payload["recent_log_lines"] == log_lines[-20:]
    assert prompt_payload["incident_summary"]["counts_by_category"]["websocket_drop"] == 1
    assert prompt_payload["incident_summary"]["counts_by_category"]["order_failure"] == 1

    audit = analyzer.last_audit_record
    assert audit is not None
    assert audit.model_name == payload["model"]
    assert audit.validation_passed is True
    assert audit.final_action is AllowedAction.RETRY
    assert audit.raw_json_output == advice.raw_output
    assert len(audit.prompt_hash) == 64


def test_timeout_falls_back_to_escalate() -> None:
    with patch("healing.ollama_analyzer.urlopen", side_effect=socket.timeout("timed out")):
        analyzer = OllamaAnalyzer()
        advice = analyzer.analyze(_snapshot(), [], [])

    assert advice.recommended_action is AllowedAction.ESCALATE
    assert advice.confidence == 0.0
    error_payload = json.loads(advice.raw_output)
    assert error_payload["error_type"] == "OllamaTimeoutError"

    audit = analyzer.last_audit_record
    assert audit is not None
    assert audit.validation_passed is False
    assert audit.final_action is AllowedAction.ESCALATE


def test_malformed_response_falls_back_to_escalate() -> None:
    with patch(
        "healing.ollama_analyzer.urlopen",
        return_value=_MockHTTPResponse(json.dumps({"response": "not json"})),
    ):
        analyzer = OllamaAnalyzer()
        advice = analyzer.analyze(_snapshot(), [], ["heartbeat ok"])

    assert advice.recommended_action is AllowedAction.ESCALATE
    assert advice.confidence == 0.0
    assert advice.raw_output == json.dumps({"response": "not json"})

    audit = analyzer.last_audit_record
    assert audit is not None
    assert audit.validation_passed is False


@pytest.mark.parametrize(
    "structured",
    [
        {
            "recommended_action": "invalid_action",
            "confidence": 0.5,
            "reasoning": "Bad action.",
            "raw_output": "invalid",
        },
        {
            "recommended_action": "retry",
            "confidence": 1.4,
            "reasoning": "Too confident.",
            "raw_output": "invalid",
        },
        {
            "recommended_action": "retry",
            "confidence": 0.4,
            "reasoning": "Missing raw output.",
        },
        {
            "recommended_action": "retry",
            "confidence": 0.4,
            "reasoning": "Unexpected field.",
            "raw_output": "invalid",
            "extra_field": "not allowed",
        },
    ],
)
def test_schema_validation_falls_back_for_invalid_structured_fields(
    structured: dict[str, object],
) -> None:
    with patch(
        "healing.ollama_analyzer.urlopen",
        return_value=_MockHTTPResponse(_outer_response(structured)),
    ):
        analyzer = OllamaAnalyzer()
        advice = analyzer.analyze(_snapshot(), [_incident()], ["websocket disconnected"])

    assert advice.recommended_action is AllowedAction.ESCALATE
    assert advice.confidence == 0.0

    audit = analyzer.last_audit_record
    assert audit is not None
    assert audit.validation_passed is False
    assert audit.final_action is AllowedAction.ESCALATE
    assert audit.raw_json_output == advice.raw_output
    raw_response = json.loads(advice.raw_output)
    assert json.loads(raw_response["response"]) == structured
