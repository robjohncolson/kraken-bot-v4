from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from healing.analyzer import (
    DEFAULT_ANALYZER,
    DeterministicAnalyzer,
    InvalidWatchdogAdviceError,
    WatchdogAdvice,
    WatchdogAnalyzer,
)
from healing.heartbeat import HeartbeatSnapshot, HeartbeatStatus
from healing.incidents import AllowedAction, Incident, IncidentCategory, IncidentSeverity


def _snapshot(
    *,
    bot_status: HeartbeatStatus = HeartbeatStatus.HEALTHY,
    websocket_connected: bool = True,
) -> HeartbeatSnapshot:
    return HeartbeatSnapshot(
        timestamp=datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc),
        bot_status=bot_status,
        active_positions_count=2,
        open_orders_count=5,
        last_reconciliation_age_sec=12.5,
        last_belief_age_sec=45.0,
        websocket_connected=websocket_connected,
        supabase_connected=True,
    )


def _incident(
    *,
    category: IncidentCategory,
    severity: IncidentSeverity = IncidentSeverity.MEDIUM,
) -> Incident:
    return Incident(
        incident_id=uuid4(),
        timestamp=datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc),
        severity=severity,
        category=category,
        description=f"{category.value} detected.",
        context={"pair": "DOGE/USD"},
        recommended_action=AllowedAction.NOOP,
    )


def test_watchdog_advice_validates_fields() -> None:
    advice = WatchdogAdvice(
        recommended_action="retry",
        confidence=0.75,
        reasoning="Retry the data feed.",
        raw_output='{"matched_rule":"websocket_drop"}',
    )

    assert advice.recommended_action is AllowedAction.RETRY
    assert advice.confidence == 0.75

    with pytest.raises(InvalidWatchdogAdviceError):
        WatchdogAdvice(
            recommended_action=AllowedAction.RETRY,
            confidence=1.1,
            reasoning="Too confident.",
            raw_output="{}",
        )


def test_deterministic_analyzer_satisfies_protocol_contract() -> None:
    analyzer = DeterministicAnalyzer()

    assert isinstance(analyzer, WatchdogAnalyzer)
    assert isinstance(DEFAULT_ANALYZER, WatchdogAnalyzer)


def test_stale_heartbeat_escalates() -> None:
    advice = DeterministicAnalyzer().analyze(None, [], [])

    assert advice.recommended_action is AllowedAction.ESCALATE
    assert advice.confidence == 1.0
    assert json.loads(advice.raw_output)["matched_rule"] == "stale_heartbeat"


def test_stopped_heartbeat_escalates() -> None:
    advice = DeterministicAnalyzer().analyze(_snapshot(bot_status=HeartbeatStatus.STOPPED), [], [])

    assert advice.recommended_action is AllowedAction.ESCALATE
    assert json.loads(advice.raw_output)["matched_rule"] == "stopped_status"


def test_repeated_order_failures_pause_component() -> None:
    incidents = [
        _incident(category=IncidentCategory.ORDER_FAILURE),
        _incident(category=IncidentCategory.ORDER_FAILURE),
        _incident(category=IncidentCategory.ORDER_FAILURE),
    ]

    advice = DeterministicAnalyzer().analyze(_snapshot(), incidents, [])

    assert advice.recommended_action is AllowedAction.PAUSE_COMPONENT
    assert json.loads(advice.raw_output)["matched_rule"] == "repeated_order_failures"


def test_reconciliation_mismatch_runs_reconcile() -> None:
    incidents = [_incident(category=IncidentCategory.RECONCILIATION_MISMATCH)]

    advice = DeterministicAnalyzer().analyze(_snapshot(), incidents, [])

    assert advice.recommended_action is AllowedAction.RUN_RECONCILE
    assert json.loads(advice.raw_output)["matched_rule"] == "reconciliation_mismatch"


def test_websocket_drop_retries_from_logs() -> None:
    advice = DeterministicAnalyzer().analyze(
        _snapshot(),
        [],
        ["WebSocket disconnected while reading ticker feed."],
    )

    assert advice.recommended_action is AllowedAction.RETRY
    assert json.loads(advice.raw_output)["matched_rule"] == "websocket_drop"


def test_degraded_heartbeat_escalates() -> None:
    advice = DeterministicAnalyzer().analyze(_snapshot(bot_status=HeartbeatStatus.DEGRADED), [], [])

    assert advice.recommended_action is AllowedAction.ESCALATE
    assert json.loads(advice.raw_output)["matched_rule"] == "degraded_status"


def test_healthy_inputs_return_noop() -> None:
    advice = DEFAULT_ANALYZER.analyze(_snapshot(), [], ["Heartbeat refreshed successfully."])

    assert advice.recommended_action is AllowedAction.NOOP
    assert json.loads(advice.raw_output)["matched_rule"] == "healthy_noop"
