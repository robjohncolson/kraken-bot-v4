from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from core.errors import KrakenBotError
from healing.heartbeat import HeartbeatSnapshot, HeartbeatStatus
from healing.incidents import AllowedAction, Incident, IncidentCategory

_WEBSOCKET_LOG_FRAGMENTS = (
    "websocket drop",
    "websocket disconnected",
    "websocket closed",
    "lost websocket",
    "ws disconnected",
)


class AnalyzerError(KrakenBotError):
    """Base exception for watchdog analyzer failures."""


class InvalidWatchdogAdviceError(AnalyzerError):
    """Raised when watchdog advice is structurally invalid."""


@dataclass(frozen=True, slots=True)
class WatchdogAdvice:
    recommended_action: AllowedAction
    confidence: float
    reasoning: str
    raw_output: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "recommended_action", _coerce_action(self.recommended_action))
        object.__setattr__(self, "confidence", _coerce_confidence(self.confidence))
        object.__setattr__(self, "reasoning", _coerce_reasoning(self.reasoning))
        object.__setattr__(self, "raw_output", _coerce_raw_output(self.raw_output))


@runtime_checkable
class WatchdogAnalyzer(Protocol):
    def analyze(
        self,
        heartbeat: HeartbeatSnapshot | None,
        recent_incidents: Sequence[Incident],
        recent_log_lines: Sequence[str],
    ) -> WatchdogAdvice:
        """Analyze watchdog inputs and return advisory action."""


class DeterministicAnalyzer:
    """Pure rule-based fallback analyzer used when no model is available."""

    ORDER_FAILURE_THRESHOLD = 3

    def analyze(
        self,
        heartbeat: HeartbeatSnapshot | None,
        recent_incidents: Sequence[Incident],
        recent_log_lines: Sequence[str],
    ) -> WatchdogAdvice:
        incident_counts = _count_incidents(recent_incidents)

        if heartbeat is None:
            return _build_advice(
                recommended_action=AllowedAction.ESCALATE,
                confidence=1.0,
                reasoning="Heartbeat is missing or stale; watchdog cannot verify bot liveness.",
                matched_rule="stale_heartbeat",
                heartbeat=heartbeat,
                incident_counts=incident_counts,
                recent_log_lines=recent_log_lines,
            )

        if heartbeat.bot_status is HeartbeatStatus.STOPPED:
            return _build_advice(
                recommended_action=AllowedAction.ESCALATE,
                confidence=0.95,
                reasoning="Bot reported a stopped status and requires operator attention.",
                matched_rule="stopped_status",
                heartbeat=heartbeat,
                incident_counts=incident_counts,
                recent_log_lines=recent_log_lines,
            )

        if incident_counts[IncidentCategory.ORDER_FAILURE] >= self.ORDER_FAILURE_THRESHOLD:
            return _build_advice(
                recommended_action=AllowedAction.PAUSE_COMPONENT,
                confidence=0.95,
                reasoning="Repeated order failures suggest the trading component should pause before retrying.",
                matched_rule="repeated_order_failures",
                heartbeat=heartbeat,
                incident_counts=incident_counts,
                recent_log_lines=recent_log_lines,
            )

        if incident_counts[IncidentCategory.RECONCILIATION_MISMATCH] > 0:
            return _build_advice(
                recommended_action=AllowedAction.RUN_RECONCILE,
                confidence=0.9,
                reasoning="A recent reconciliation mismatch was detected and should be rechecked.",
                matched_rule="reconciliation_mismatch",
                heartbeat=heartbeat,
                incident_counts=incident_counts,
                recent_log_lines=recent_log_lines,
            )

        if (
            incident_counts[IncidentCategory.WEBSOCKET_DROP] > 0
            or not heartbeat.websocket_connected
            or _logs_indicate_websocket_drop(recent_log_lines)
        ):
            return _build_advice(
                recommended_action=AllowedAction.RETRY,
                confidence=0.8,
                reasoning="WebSocket connectivity appears impaired and a retry is the narrowest safe action.",
                matched_rule="websocket_drop",
                heartbeat=heartbeat,
                incident_counts=incident_counts,
                recent_log_lines=recent_log_lines,
            )

        if heartbeat.bot_status is HeartbeatStatus.DEGRADED:
            return _build_advice(
                recommended_action=AllowedAction.ESCALATE,
                confidence=0.65,
                reasoning="Bot reported a degraded state without a narrower deterministic remediation.",
                matched_rule="degraded_status",
                heartbeat=heartbeat,
                incident_counts=incident_counts,
                recent_log_lines=recent_log_lines,
            )

        return _build_advice(
            recommended_action=AllowedAction.NOOP,
            confidence=0.85,
            reasoning="No deterministic watchdog rule matched the current heartbeat, incidents, or logs.",
            matched_rule="healthy_noop",
            heartbeat=heartbeat,
            incident_counts=incident_counts,
            recent_log_lines=recent_log_lines,
        )


DEFAULT_ANALYZER: WatchdogAnalyzer = DeterministicAnalyzer()


def _build_advice(
    *,
    recommended_action: AllowedAction,
    confidence: float,
    reasoning: str,
    matched_rule: str,
    heartbeat: HeartbeatSnapshot | None,
    incident_counts: dict[IncidentCategory, int],
    recent_log_lines: Sequence[str],
) -> WatchdogAdvice:
    raw_output = json.dumps(
        {
            "source": "deterministic",
            "matched_rule": matched_rule,
            "recommended_action": recommended_action.value,
            "confidence": confidence,
            "heartbeat": None if heartbeat is None else heartbeat.to_record(),
            "incident_counts": {
                category.value: count for category, count in incident_counts.items() if count > 0
            },
            "recent_log_lines": [str(line) for line in recent_log_lines[-5:]],
        },
        sort_keys=True,
    )
    return WatchdogAdvice(
        recommended_action=recommended_action,
        confidence=confidence,
        reasoning=reasoning,
        raw_output=raw_output,
    )


def _count_incidents(recent_incidents: Sequence[Incident]) -> dict[IncidentCategory, int]:
    counts = {category: 0 for category in IncidentCategory}
    for incident in recent_incidents:
        counts[incident.category] += 1
    return counts


def _logs_indicate_websocket_drop(recent_log_lines: Sequence[str]) -> bool:
    normalized_lines = [str(line).strip().lower() for line in recent_log_lines]
    return any(
        any(fragment in line for fragment in _WEBSOCKET_LOG_FRAGMENTS)
        or ("websocket" in line and "disconnect" in line)
        for line in normalized_lines
    )


def _coerce_action(raw_value: AllowedAction | str) -> AllowedAction:
    if isinstance(raw_value, AllowedAction):
        return raw_value
    try:
        return AllowedAction(str(raw_value))
    except ValueError as exc:
        raise InvalidWatchdogAdviceError(
            f"Unsupported watchdog recommended_action {raw_value!r}."
        ) from exc


def _coerce_confidence(raw_value: object) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise InvalidWatchdogAdviceError("Watchdog confidence must be a number between 0 and 1.")
    confidence = float(raw_value)
    if confidence < 0 or confidence > 1:
        raise InvalidWatchdogAdviceError("Watchdog confidence must be between 0 and 1.")
    return confidence


def _coerce_reasoning(raw_value: object) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise InvalidWatchdogAdviceError("Watchdog reasoning must be a non-empty string.")
    return raw_value


def _coerce_raw_output(raw_value: object) -> str:
    if not isinstance(raw_value, str):
        raise InvalidWatchdogAdviceError("Watchdog raw_output must be a string.")
    return raw_value


__all__ = [
    "AnalyzerError",
    "DEFAULT_ANALYZER",
    "DeterministicAnalyzer",
    "InvalidWatchdogAdviceError",
    "WatchdogAdvice",
    "WatchdogAnalyzer",
]
