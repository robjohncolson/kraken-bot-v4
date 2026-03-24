from __future__ import annotations

import hashlib
import json
import os
import socket
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.errors import KrakenBotError
from healing.analyzer import (
    InvalidWatchdogAdviceError,
    WatchdogAdvice,
    WatchdogAnalyzer,
)
from healing.heartbeat import HeartbeatSnapshot
from healing.incidents import AllowedAction, Incident, IncidentCategory

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_LOG_LINES = 20
MAX_INCIDENT_PREVIEW = 5
OLLAMA_URL_ENV_VAR = "OLLAMA_URL"
OLLAMA_MODEL_ENV_VAR = "OLLAMA_MODEL"
_RESPONSE_FIELDS = frozenset({"recommended_action", "confidence", "reasoning", "raw_output"})

WATCHDOG_ADVICE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "recommended_action": {
            "type": "string",
            "enum": [action.value for action in AllowedAction],
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
        },
        "reasoning": {"type": "string"},
        "raw_output": {"type": "string"},
    },
    "required": ["recommended_action", "confidence", "reasoning", "raw_output"],
    "additionalProperties": False,
}


class OllamaAnalyzerError(KrakenBotError):
    """Base exception for the optional Ollama watchdog adapter."""


class OllamaConfigurationError(OllamaAnalyzerError):
    """Raised when analyzer configuration is invalid."""


class OllamaTransportError(OllamaAnalyzerError):
    """Raised when Ollama cannot be reached successfully."""


class OllamaTimeoutError(OllamaTransportError):
    """Raised when an Ollama request exceeds the configured timeout."""


class InvalidOllamaResponseError(OllamaAnalyzerError):
    """Raised when Ollama returns a malformed structured response."""


class InvalidAuditRecordError(OllamaAnalyzerError):
    """Raised when an audit record cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class AuditRecord:
    prompt_hash: str
    model_name: str
    timestamp: datetime
    raw_json_output: str
    validation_passed: bool
    final_action: AllowedAction

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "prompt_hash",
            _coerce_non_empty_string(
                self.prompt_hash,
                field_name="prompt_hash",
                error_type=InvalidAuditRecordError,
            ),
        )
        object.__setattr__(
            self,
            "model_name",
            _coerce_non_empty_string(
                self.model_name,
                field_name="model_name",
                error_type=InvalidAuditRecordError,
            ),
        )
        object.__setattr__(self, "timestamp", _normalize_timestamp(self.timestamp))
        object.__setattr__(
            self,
            "raw_json_output",
            _coerce_string(self.raw_json_output, field_name="raw_json_output"),
        )
        if not isinstance(self.validation_passed, bool):
            raise InvalidAuditRecordError("validation_passed must be a boolean.")
        object.__setattr__(self, "final_action", _coerce_action(self.final_action))


class OllamaAnalyzer:
    """Optional WatchdogAnalyzer backed by a local Ollama instance."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        configured_url = (
            _read_env_or_default(OLLAMA_URL_ENV_VAR, DEFAULT_OLLAMA_URL)
            if base_url is None
            else base_url
        )
        configured_model = (
            _read_env_or_default(OLLAMA_MODEL_ENV_VAR, DEFAULT_OLLAMA_MODEL)
            if model_name is None
            else model_name
        )
        self._base_url = _normalize_base_url(configured_url)
        self._model_name = _coerce_non_empty_string(configured_model, field_name="model_name")
        self._timeout_seconds = _coerce_timeout(timeout_seconds)
        self._last_audit_record: AuditRecord | None = None

    @property
    def last_audit_record(self) -> AuditRecord | None:
        return self._last_audit_record

    def analyze(
        self,
        heartbeat: HeartbeatSnapshot | None,
        recent_incidents: Sequence[Incident],
        recent_log_lines: Sequence[str],
    ) -> WatchdogAdvice:
        timestamp = datetime.now(timezone.utc)
        prompt = _build_prompt(heartbeat, recent_incidents, recent_log_lines)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        raw_json_output = ""

        try:
            raw_json_output = self._request_ollama(prompt)
            structured = _parse_structured_response(raw_json_output)
            validated = _validate_structured_advice(structured)
        except (
            InvalidOllamaResponseError,
            InvalidWatchdogAdviceError,
            OllamaTimeoutError,
            OllamaTransportError,
        ) as exc:
            fallback_raw_output = raw_json_output or _serialize_error_payload(exc)
            advice = _build_fallback_advice(exc, fallback_raw_output)
            self._last_audit_record = AuditRecord(
                prompt_hash=prompt_hash,
                model_name=self._model_name,
                timestamp=timestamp,
                raw_json_output=fallback_raw_output,
                validation_passed=False,
                final_action=advice.recommended_action,
            )
            return advice

        advice = WatchdogAdvice(
            recommended_action=validated.recommended_action,
            confidence=validated.confidence,
            reasoning=validated.reasoning,
            raw_output=raw_json_output,
        )
        self._last_audit_record = AuditRecord(
            prompt_hash=prompt_hash,
            model_name=self._model_name,
            timestamp=timestamp,
            raw_json_output=raw_json_output,
            validation_passed=True,
            final_action=advice.recommended_action,
        )
        return advice

    def _request_ollama(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self._model_name,
                "prompt": prompt,
                "stream": False,
                "format": WATCHDOG_ADVICE_SCHEMA,
                "options": {"temperature": 0},
            },
            sort_keys=True,
        ).encode("utf-8")
        request = Request(
            url=f"{self._base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            raise OllamaTransportError(f"Ollama returned HTTP {exc.code}.") from exc
        except URLError as exc:
            if _is_timeout_reason(exc.reason):
                raise OllamaTimeoutError(
                    f"Ollama request timed out after {self._timeout_seconds} seconds."
                ) from exc
            raise OllamaTransportError("Failed to reach Ollama.") from exc
        except TimeoutError as exc:
            raise OllamaTimeoutError(
                f"Ollama request timed out after {self._timeout_seconds} seconds."
            ) from exc
        except socket.timeout as exc:
            raise OllamaTimeoutError(
                f"Ollama request timed out after {self._timeout_seconds} seconds."
            ) from exc
        except UnicodeDecodeError as exc:
            raise InvalidOllamaResponseError("Ollama response was not valid UTF-8.") from exc


def _build_prompt(
    heartbeat: HeartbeatSnapshot | None,
    recent_incidents: Sequence[Incident],
    recent_log_lines: Sequence[str],
) -> str:
    prompt_payload = {
        "task": "watchdog_action_classification",
        "instructions": [
            "Return JSON only.",
            "Choose recommended_action from allowed_actions.",
            "Set confidence to a number between 0 and 1.",
            "Keep reasoning to one short sentence without chain-of-thought.",
            "Set raw_output to a short evidence snippet copied from the input.",
        ],
        "allowed_actions": [action.value for action in AllowedAction],
        "heartbeat_snapshot": None if heartbeat is None else heartbeat.to_record(),
        "incident_summary": _summarize_incidents(recent_incidents),
        "recent_log_lines": [str(line) for line in recent_log_lines[-MAX_LOG_LINES:]],
    }
    return json.dumps(prompt_payload, sort_keys=True)


def _summarize_incidents(recent_incidents: Sequence[Incident]) -> dict[str, object]:
    category_counts = {category.value: 0 for category in IncidentCategory}
    action_counts = {action.value: 0 for action in AllowedAction}
    preview: list[dict[str, object]] = []

    for incident in recent_incidents:
        category_counts[incident.category.value] += 1
        action_counts[incident.recommended_action.value] += 1
    for incident in list(recent_incidents)[:MAX_INCIDENT_PREVIEW]:
        preview.append(
            {
                "timestamp": incident.timestamp.isoformat().replace("+00:00", "Z"),
                "severity": incident.severity.value,
                "category": incident.category.value,
                "recommended_action": incident.recommended_action.value,
                "description": incident.description,
            }
        )

    return {
        "total_recent_incidents": len(recent_incidents),
        "counts_by_category": category_counts,
        "counts_by_recommended_action": action_counts,
        "recent_incidents": preview,
    }


def _parse_structured_response(raw_json_output: str) -> Mapping[str, object]:
    try:
        envelope = json.loads(raw_json_output)
    except json.JSONDecodeError as exc:
        raise InvalidOllamaResponseError("Ollama returned invalid JSON.") from exc
    if not isinstance(envelope, Mapping):
        raise InvalidOllamaResponseError("Ollama response must decode to a JSON object.")

    structured_payload = envelope.get("response")
    if not isinstance(structured_payload, str) or not structured_payload.strip():
        raise InvalidOllamaResponseError("Ollama response missing structured output.")

    try:
        decoded = json.loads(structured_payload)
    except json.JSONDecodeError as exc:
        raise InvalidOllamaResponseError("Ollama structured output was not valid JSON.") from exc
    if not isinstance(decoded, Mapping):
        raise InvalidOllamaResponseError("Ollama structured output must decode to a JSON object.")
    if any(not isinstance(key, str) for key in decoded):
        raise InvalidOllamaResponseError("Ollama structured output keys must be strings.")
    if any(key not in _RESPONSE_FIELDS for key in decoded):
        raise InvalidOllamaResponseError("Ollama structured output included unexpected fields.")
    return decoded


def _validate_structured_advice(structured: Mapping[str, object]) -> WatchdogAdvice:
    return WatchdogAdvice(
        recommended_action=structured.get("recommended_action"),
        confidence=structured.get("confidence"),
        reasoning=structured.get("reasoning"),
        raw_output=structured.get("raw_output"),
    )


def _build_fallback_advice(exc: BaseException, raw_output: str) -> WatchdogAdvice:
    if isinstance(exc, OllamaTimeoutError):
        reasoning = "Ollama watchdog analysis timed out; escalate for manual review."
    elif isinstance(exc, OllamaTransportError):
        reasoning = "Ollama watchdog analysis is unavailable; escalate for manual review."
    else:
        reasoning = "Ollama watchdog output was invalid; escalate for manual review."
    return WatchdogAdvice(
        recommended_action=AllowedAction.ESCALATE,
        confidence=0.0,
        reasoning=reasoning,
        raw_output=raw_output,
    )


def _serialize_error_payload(exc: BaseException) -> str:
    return json.dumps(
        {
            "error_type": type(exc).__name__,
            "message": str(exc),
        },
        sort_keys=True,
    )


def _read_env_or_default(name: str, default: str) -> str:
    raw_value = os.getenv(name)
    if raw_value is None or not raw_value.strip():
        return default
    return raw_value


def _normalize_base_url(raw_value: object) -> str:
    base_url = _coerce_non_empty_string(raw_value, field_name="base_url").rstrip("/")
    if not base_url:
        raise OllamaConfigurationError("base_url must be non-empty.")
    return base_url


def _coerce_timeout(raw_value: object) -> float:
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise OllamaConfigurationError("timeout_seconds must be a positive number.")
    timeout_seconds = float(raw_value)
    if timeout_seconds <= 0:
        raise OllamaConfigurationError("timeout_seconds must be a positive number.")
    return timeout_seconds


def _normalize_timestamp(timestamp: datetime) -> datetime:
    if not isinstance(timestamp, datetime):
        raise InvalidAuditRecordError("timestamp must be a datetime.")
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _coerce_action(raw_value: AllowedAction | str) -> AllowedAction:
    if isinstance(raw_value, AllowedAction):
        return raw_value
    try:
        return AllowedAction(
            _coerce_non_empty_string(
                raw_value,
                field_name="final_action",
                error_type=InvalidAuditRecordError,
            )
        )
    except ValueError as exc:
        raise InvalidAuditRecordError(f"Unsupported final_action {raw_value!r}.") from exc


def _coerce_non_empty_string(
    raw_value: object,
    *,
    field_name: str,
    error_type: type[OllamaAnalyzerError] = OllamaConfigurationError,
) -> str:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise error_type(f"{field_name} must be a non-empty string.")
    return raw_value


def _coerce_string(raw_value: object, *, field_name: str) -> str:
    if not isinstance(raw_value, str):
        raise InvalidAuditRecordError(f"{field_name} must be a string.")
    return raw_value


def _is_timeout_reason(reason: object) -> bool:
    return isinstance(reason, (TimeoutError, socket.timeout))


__all__ = [
    "AuditRecord",
    "DEFAULT_OLLAMA_MODEL",
    "DEFAULT_OLLAMA_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "InvalidAuditRecordError",
    "InvalidOllamaResponseError",
    "OllamaAnalyzer",
    "OllamaAnalyzerError",
    "OllamaConfigurationError",
    "OllamaTimeoutError",
    "OllamaTransportError",
    "WATCHDOG_ADVICE_SCHEMA",
]
