from __future__ import annotations

import hashlib
import json
import os
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from core.errors import KrakenBotError
from healing.analyzer import DEFAULT_ANALYZER, WatchdogAdvice, WatchdogAnalyzer
from healing.heartbeat import (
    DEFAULT_HEARTBEAT_PATH,
    DEFAULT_STALE_AFTER_SECONDS,
    HeartbeatSnapshot,
    HeartbeatStatus,
    read_heartbeat,
)
from healing.incidents import (
    DEFAULT_INCIDENTS_DIR,
    AllowedAction,
    Incident,
    IncidentCategory,
    IncidentRegistry,
    IncidentSeverity,
)
from healing.ollama_analyzer import (
    DEFAULT_TIMEOUT_SECONDS,
    OLLAMA_URL_ENV_VAR,
    AuditRecord,
    OllamaAnalyzer,
)

DEFAULT_AUDIT_PATH = Path("state") / "watchdog-audit.ndjson"
DEFAULT_LOG_PATH = Path("state") / "bot.log"
DEFAULT_RECENT_INCIDENT_LIMIT = 10
DEFAULT_RECENT_LOG_LINE_LIMIT = 20


class WatchdogError(KrakenBotError):
    """Base exception for watchdog orchestration failures."""


class WatchdogIOError(WatchdogError):
    """Raised when watchdog state files cannot be read or written."""


@dataclass(frozen=True, slots=True)
class WatchdogResult:
    heartbeat_status: HeartbeatStatus | None
    analyzer_used: str
    advice: WatchdogAdvice
    new_incident: Incident | None

    def __post_init__(self) -> None:
        if self.heartbeat_status is not None and not isinstance(self.heartbeat_status, HeartbeatStatus):
            raise WatchdogError("heartbeat_status must be a HeartbeatStatus or None.")
        if not isinstance(self.analyzer_used, str) or not self.analyzer_used.strip():
            raise WatchdogError("analyzer_used must be a non-empty string.")
        if not isinstance(self.advice, WatchdogAdvice):
            raise WatchdogError("advice must be a WatchdogAdvice.")
        if self.new_incident is not None and not isinstance(self.new_incident, Incident):
            raise WatchdogError("new_incident must be an Incident or None.")


class Watchdog:
    """Advisory-only watchdog for phase 6 self-healing."""

    def __init__(
        self,
        *,
        heartbeat_path: Path = DEFAULT_HEARTBEAT_PATH,
        incident_registry: IncidentRegistry | None = None,
        audit_path: Path = DEFAULT_AUDIT_PATH,
        log_path: Path = DEFAULT_LOG_PATH,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        recent_incident_limit: int = DEFAULT_RECENT_INCIDENT_LIMIT,
        recent_log_line_limit: int = DEFAULT_RECENT_LOG_LINE_LIMIT,
        ollama_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._heartbeat_path = heartbeat_path
        self._incident_registry = incident_registry or IncidentRegistry(DEFAULT_INCIDENTS_DIR)
        self._audit_path = audit_path
        self._log_path = log_path
        self._stale_after_seconds = stale_after_seconds
        self._recent_incident_limit = recent_incident_limit
        self._recent_log_line_limit = recent_log_line_limit
        self._ollama_timeout_seconds = ollama_timeout_seconds

    def check(self) -> WatchdogResult:
        heartbeat = read_heartbeat(
            self._heartbeat_path,
            stale_after_seconds=self._stale_after_seconds,
        )
        recent_incidents = self._incident_registry.get_recent(self._recent_incident_limit)
        recent_log_lines = self._read_recent_log_lines()
        analyzer_used, analyzer = self._select_analyzer()
        advice = analyzer.analyze(heartbeat, recent_incidents, recent_log_lines)
        self._append_audit_line(
            analyzer_used=analyzer_used,
            heartbeat=heartbeat,
            recent_incidents=recent_incidents,
            recent_log_lines=recent_log_lines,
            advice=advice,
            analyzer=analyzer,
        )

        new_incident = self._record_advisory_incident(
            heartbeat=heartbeat,
            recent_incidents=recent_incidents,
            analyzer_used=analyzer_used,
            advice=advice,
        )
        return WatchdogResult(
            heartbeat_status=None if heartbeat is None else heartbeat.bot_status,
            analyzer_used=analyzer_used,
            advice=advice,
            new_incident=new_incident,
        )

    def _select_analyzer(self) -> tuple[str, WatchdogAnalyzer]:
        configured_url = os.getenv(OLLAMA_URL_ENV_VAR)
        if configured_url and configured_url.strip():
            normalized_url = configured_url.strip().rstrip("/")
            if _ollama_is_reachable(normalized_url, timeout_seconds=self._ollama_timeout_seconds):
                return "ollama", OllamaAnalyzer(
                    base_url=normalized_url,
                    timeout_seconds=self._ollama_timeout_seconds,
                )
        return "deterministic", DEFAULT_ANALYZER

    def _read_recent_log_lines(self) -> list[str]:
        try:
            raw_lines = self._log_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise WatchdogIOError(f"Failed to read watchdog log lines from {self._log_path}.") from exc
        return raw_lines[-self._recent_log_line_limit :]

    def _append_audit_line(
        self,
        *,
        analyzer_used: str,
        heartbeat: HeartbeatSnapshot | None,
        recent_incidents: Sequence[Incident],
        recent_log_lines: Sequence[str],
        advice: WatchdogAdvice,
        analyzer: WatchdogAnalyzer,
    ) -> None:
        audit_record = self._resolve_audit_record(
            analyzer_used=analyzer_used,
            heartbeat=heartbeat,
            recent_incidents=recent_incidents,
            recent_log_lines=recent_log_lines,
            advice=advice,
            analyzer=analyzer,
        )
        payload = json.dumps(
            {
                "prompt_hash": audit_record.prompt_hash,
                "model": audit_record.model_name,
                "timestamp": audit_record.timestamp.isoformat().replace("+00:00", "Z"),
                "raw_output": audit_record.raw_json_output,
                "validation": {"passed": audit_record.validation_passed},
                "validation_passed": audit_record.validation_passed,
                "final_decision": audit_record.final_action.value,
                "analyzer_used": analyzer_used,
            },
            sort_keys=True,
        )
        self._audit_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except OSError as exc:
            raise WatchdogIOError(f"Failed to append watchdog audit trail to {self._audit_path}.") from exc

    def _resolve_audit_record(
        self,
        *,
        analyzer_used: str,
        heartbeat: HeartbeatSnapshot | None,
        recent_incidents: Sequence[Incident],
        recent_log_lines: Sequence[str],
        advice: WatchdogAdvice,
        analyzer: WatchdogAnalyzer,
    ) -> AuditRecord:
        existing_record = getattr(analyzer, "last_audit_record", None)
        if isinstance(existing_record, AuditRecord):
            return existing_record

        prompt_hash = _build_prompt_hash(heartbeat, recent_incidents, recent_log_lines)
        return AuditRecord(
            prompt_hash=prompt_hash,
            model_name=analyzer_used,
            timestamp=datetime.now(timezone.utc),
            raw_json_output=advice.raw_output,
            validation_passed=True,
            final_action=advice.recommended_action,
        )

    def _record_advisory_incident(
        self,
        *,
        heartbeat: HeartbeatSnapshot | None,
        recent_incidents: Sequence[Incident],
        analyzer_used: str,
        advice: WatchdogAdvice,
    ) -> Incident | None:
        if advice.recommended_action is AllowedAction.NOOP:
            return None

        incident = Incident(
            incident_id=uuid4(),
            timestamp=datetime.now(timezone.utc),
            severity=_select_incident_severity(heartbeat, advice),
            category=_select_incident_category(heartbeat, recent_incidents, advice),
            description=advice.reasoning,
            context={
                "analyzer_used": analyzer_used,
                "confidence": advice.confidence,
                "heartbeat": None if heartbeat is None else heartbeat.to_record(),
                "recent_incident_ids": [str(incident.incident_id) for incident in recent_incidents[:5]],
            },
            recommended_action=advice.recommended_action,
        )
        self._incident_registry.record_incident(incident)
        return incident


def _build_prompt_hash(
    heartbeat: HeartbeatSnapshot | None,
    recent_incidents: Sequence[Incident],
    recent_log_lines: Sequence[str],
) -> str:
    payload = json.dumps(
        {
            "heartbeat": None if heartbeat is None else heartbeat.to_record(),
            "recent_incidents": [
                {
                    "incident_id": str(incident.incident_id),
                    "timestamp": incident.timestamp.isoformat().replace("+00:00", "Z"),
                    "severity": incident.severity.value,
                    "category": incident.category.value,
                    "recommended_action": incident.recommended_action.value,
                }
                for incident in recent_incidents
            ],
            "recent_log_lines": [str(line) for line in recent_log_lines],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _select_incident_severity(
    heartbeat: HeartbeatSnapshot | None,
    advice: WatchdogAdvice,
) -> IncidentSeverity:
    if heartbeat is None or (heartbeat.bot_status is HeartbeatStatus.STOPPED):
        return IncidentSeverity.CRITICAL
    if advice.recommended_action in {AllowedAction.ESCALATE, AllowedAction.PAUSE_COMPONENT}:
        return IncidentSeverity.HIGH
    return IncidentSeverity.MEDIUM


def _select_incident_category(
    heartbeat: HeartbeatSnapshot | None,
    recent_incidents: Sequence[Incident],
    advice: WatchdogAdvice,
) -> IncidentCategory:
    if heartbeat is not None and not heartbeat.supabase_connected:
        return IncidentCategory.SUPABASE_UNAVAILABLE
    if heartbeat is not None and not heartbeat.websocket_connected:
        return IncidentCategory.WEBSOCKET_DROP
    if advice.recommended_action is AllowedAction.RUN_RECONCILE:
        return IncidentCategory.RECONCILIATION_MISMATCH
    if advice.recommended_action is AllowedAction.PAUSE_COMPONENT:
        return IncidentCategory.ORDER_FAILURE
    if advice.recommended_action is AllowedAction.RETRY:
        return IncidentCategory.WEBSOCKET_DROP
    if any(incident.category is IncidentCategory.ORDER_FAILURE for incident in recent_incidents):
        return IncidentCategory.ORDER_FAILURE
    if any(incident.category is IncidentCategory.RECONCILIATION_MISMATCH for incident in recent_incidents):
        return IncidentCategory.RECONCILIATION_MISMATCH
    if any(incident.category is IncidentCategory.WEBSOCKET_DROP for incident in recent_incidents):
        return IncidentCategory.WEBSOCKET_DROP
    return IncidentCategory.UNKNOWN


def _ollama_is_reachable(base_url: str, *, timeout_seconds: float) -> bool:
    request = Request(url=f"{base_url}/api/tags", method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds):
            return True
    except (HTTPError, URLError, TimeoutError, socket.timeout):
        return False


__all__ = [
    "DEFAULT_AUDIT_PATH",
    "DEFAULT_LOG_PATH",
    "Watchdog",
    "WatchdogError",
    "WatchdogIOError",
    "WatchdogResult",
]
