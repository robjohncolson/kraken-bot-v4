from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeAlias
from uuid import uuid4

from core.errors import KrakenBotError

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]

IdentifierSource = Callable[[], str]
TimestampSource = Callable[[], str]


class SupabasePersistenceError(KrakenBotError):
    """Base exception for Supabase persistence scaffolding."""


class InvalidSupabaseMutationError(SupabasePersistenceError):
    """Raised when a queued write cannot be represented safely."""


class OfflineQueueError(SupabasePersistenceError):
    """Base exception for offline queue failures."""


class InvalidOfflineQueueLimitError(OfflineQueueError):
    """Raised when dequeue is called with an invalid limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Offline queue limit must be at least 1; got {limit!r}.")


class OfflineQueueIOError(OfflineQueueError):
    """Raised when queue storage cannot be read or written."""


class OfflineQueueCorruptionError(OfflineQueueError):
    """Raised when queue contents cannot be decoded into queued writes."""


class SupabaseOperation(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class SupabaseCredentials:
    url: str
    key: str

    def __post_init__(self) -> None:
        if not self.url.strip():
            raise InvalidSupabaseMutationError("Supabase url must be non-empty.")
        if not self.key.strip():
            raise InvalidSupabaseMutationError("Supabase key must be non-empty.")


@dataclass(frozen=True, slots=True)
class SupabaseMutation:
    table: str
    payload: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))
    filters: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))
    operation: SupabaseOperation = SupabaseOperation.UPSERT

    def __post_init__(self) -> None:
        if not self.table.strip():
            raise InvalidSupabaseMutationError("Supabase table must be non-empty.")
        object.__setattr__(self, "payload", _normalize_mapping(self.payload, field_name="payload"))
        object.__setattr__(self, "filters", _normalize_mapping(self.filters, field_name="filters"))

    def to_record(self) -> dict[str, object]:
        return {
            "table": self.table,
            "operation": self.operation.value,
            "payload": _serialize_mapping(self.payload),
            "filters": _serialize_mapping(self.filters),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> SupabaseMutation:
        table = _read_string(record, "table")
        operation_raw = _read_string(record, "operation")
        try:
            operation = SupabaseOperation(operation_raw)
        except ValueError as exc:
            raise InvalidSupabaseMutationError(
                f"Unsupported Supabase operation {operation_raw!r}."
            ) from exc

        payload = _read_mapping(record, "payload")
        filters = _read_mapping(record, "filters")
        return cls(
            table=table,
            operation=operation,
            payload=payload,
            filters=filters,
        )


@dataclass(frozen=True, slots=True)
class QueuedSupabaseMutation:
    entry_id: str
    enqueued_at: str
    mutation: SupabaseMutation
    attempt_count: int = 0

    def __post_init__(self) -> None:
        if not self.entry_id.strip():
            raise InvalidSupabaseMutationError("Queued mutation entry_id must be non-empty.")
        if not self.enqueued_at.strip():
            raise InvalidSupabaseMutationError("Queued mutation enqueued_at must be non-empty.")
        if self.attempt_count < 0:
            raise InvalidSupabaseMutationError("Queued mutation attempt_count cannot be negative.")

    def to_record(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "enqueued_at": self.enqueued_at,
            "attempt_count": self.attempt_count,
            "mutation": self.mutation.to_record(),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> QueuedSupabaseMutation:
        entry_id = _read_string(record, "entry_id")
        enqueued_at = _read_string(record, "enqueued_at")
        attempt_count = _read_int(record, "attempt_count")
        mutation = SupabaseMutation.from_record(_read_mapping(record, "mutation"))
        return cls(
            entry_id=entry_id,
            enqueued_at=enqueued_at,
            attempt_count=attempt_count,
            mutation=mutation,
        )


@dataclass(frozen=True, slots=True)
class SupabaseReplayRequest:
    base_url: str
    mutation: SupabaseMutation

    @property
    def endpoint(self) -> str:
        return f"{self.base_url.rstrip('/')}/rest/v1/{self.mutation.table}"


class SupabaseTransport(Protocol):
    def execute(
        self,
        credentials: SupabaseCredentials,
        request: SupabaseReplayRequest,
    ) -> None:
        ...


class SupabaseClient:
    """Minimal client interface that accepts runtime credentials and replays queued writes."""

    def __init__(
        self,
        *,
        url: str,
        key: str,
        transport: SupabaseTransport | None = None,
    ) -> None:
        self._credentials = SupabaseCredentials(url=url, key=key)
        self._transport = transport

    @property
    def url(self) -> str:
        return self._credentials.url

    def prepare_replay(self, queued_mutation: QueuedSupabaseMutation) -> SupabaseReplayRequest:
        return SupabaseReplayRequest(
            base_url=self._credentials.url,
            mutation=queued_mutation.mutation,
        )

    def replay(self, queued_mutation: QueuedSupabaseMutation) -> SupabaseReplayRequest:
        request = self.prepare_replay(queued_mutation)
        if self._transport is not None:
            self._transport.execute(self._credentials, request)
        return request


class OfflineSupabaseQueue:
    """JSONL-backed offline queue for deferred Supabase writes."""

    def __init__(
        self,
        path: Path,
        *,
        id_source: IdentifierSource | None = None,
        timestamp_source: TimestampSource | None = None,
    ) -> None:
        self._path = path
        self._id_source = _default_id_source if id_source is None else id_source
        self._timestamp_source = _utc_timestamp if timestamp_source is None else timestamp_source

    @property
    def path(self) -> Path:
        return self._path

    def enqueue(self, mutation: SupabaseMutation) -> QueuedSupabaseMutation:
        queued_mutation = QueuedSupabaseMutation(
            entry_id=self._id_source(),
            enqueued_at=self._timestamp_source(),
            mutation=mutation,
        )
        serialized = json.dumps(queued_mutation.to_record(), separators=(",", ":"), sort_keys=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.write("\n")
        except OSError as exc:
            raise OfflineQueueIOError(f"Failed to append queued write to {self._path}.") from exc
        return queued_mutation

    def dequeue(self, *, limit: int = 1) -> tuple[QueuedSupabaseMutation, ...]:
        if limit < 1:
            raise InvalidOfflineQueueLimitError(limit)

        queued_mutations = list(self.peek_all())
        if not queued_mutations:
            return ()

        dequeued = tuple(queued_mutations[:limit])
        remaining = queued_mutations[limit:]
        self._rewrite(remaining)
        return dequeued

    def peek_all(self) -> tuple[QueuedSupabaseMutation, ...]:
        if not self._path.exists():
            return ()

        try:
            raw_lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise OfflineQueueIOError(f"Failed to read queued writes from {self._path}.") from exc

        queued_mutations: list[QueuedSupabaseMutation] = []
        for raw_line in raw_lines:
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise OfflineQueueCorruptionError(
                    f"Queued write in {self._path} is not valid JSON."
                ) from exc
            if not isinstance(record, Mapping):
                raise OfflineQueueCorruptionError(
                    f"Queued write in {self._path} must decode to an object."
                )
            try:
                queued_mutations.append(QueuedSupabaseMutation.from_record(record))
            except (InvalidSupabaseMutationError, TypeError, ValueError, KeyError) as exc:
                raise OfflineQueueCorruptionError(
                    f"Queued write in {self._path} is missing required fields."
                ) from exc
        return tuple(queued_mutations)

    def _rewrite(self, queued_mutations: list[QueuedSupabaseMutation]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        lines = [
            json.dumps(item.to_record(), separators=(",", ":"), sort_keys=True)
            for item in queued_mutations
        ]
        payload = "\n".join(lines)
        if payload:
            payload += "\n"

        try:
            temporary_path.write_text(payload, encoding="utf-8")
            temporary_path.replace(self._path)
        except OSError as exc:
            raise OfflineQueueIOError(f"Failed to rewrite queued writes at {self._path}.") from exc


def _default_id_source() -> str:
    return uuid4().hex


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_mapping(
    raw_mapping: Mapping[str, object],
    *,
    field_name: str,
) -> Mapping[str, JsonValue]:
    normalized: dict[str, JsonValue] = {}
    for raw_key, raw_value in raw_mapping.items():
        if not isinstance(raw_key, str):
            raise InvalidSupabaseMutationError(f"{field_name} keys must be strings.")
        normalized[raw_key] = _normalize_json_value(raw_value, field_name=field_name)
    return MappingProxyType(normalized)


def _normalize_json_value(raw_value: object, *, field_name: str) -> JsonValue:
    if raw_value is None or isinstance(raw_value, (str, int, float, bool)):
        return raw_value

    if isinstance(raw_value, Mapping):
        return _normalize_mapping(raw_value, field_name=field_name)

    if isinstance(raw_value, tuple):
        return tuple(_normalize_json_value(value, field_name=field_name) for value in raw_value)

    if isinstance(raw_value, list):
        return tuple(_normalize_json_value(value, field_name=field_name) for value in raw_value)

    raise InvalidSupabaseMutationError(
        f"{field_name} contains non-JSON value type {type(raw_value).__name__!r}."
    )


def _serialize_mapping(mapping_value: Mapping[str, JsonValue]) -> dict[str, object]:
    return {key: _serialize_json_value(value) for key, value in mapping_value.items()}


def _serialize_json_value(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return _serialize_mapping(value)
    if isinstance(value, tuple):
        return [_serialize_json_value(item) for item in value]
    return value


def _read_string(record: Mapping[str, object], field_name: str) -> str:
    raw_value = record[field_name]
    if not isinstance(raw_value, str):
        raise InvalidSupabaseMutationError(f"{field_name} must be a string.")
    return raw_value


def _read_int(record: Mapping[str, object], field_name: str) -> int:
    raw_value = record[field_name]
    if not isinstance(raw_value, int):
        raise InvalidSupabaseMutationError(f"{field_name} must be an integer.")
    return raw_value


def _read_mapping(record: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    raw_value = record[field_name]
    if not isinstance(raw_value, Mapping):
        raise InvalidSupabaseMutationError(f"{field_name} must be an object.")
    return raw_value


__all__ = [
    "InvalidOfflineQueueLimitError",
    "InvalidSupabaseMutationError",
    "OfflineQueueCorruptionError",
    "OfflineQueueError",
    "OfflineQueueIOError",
    "OfflineSupabaseQueue",
    "QueuedSupabaseMutation",
    "SupabaseClient",
    "SupabaseCredentials",
    "SupabaseMutation",
    "SupabaseOperation",
    "SupabasePersistenceError",
    "SupabaseReplayRequest",
    "SupabaseTransport",
]
