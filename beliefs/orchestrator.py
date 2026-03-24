from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Protocol, TypeAlias

from beliefs.consensus import ConsensusResult, compute_consensus
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    MarketRegime,
    Pair,
)

DEFAULT_STALE_AFTER = timedelta(hours=4)

logger = logging.getLogger(__name__)

SourceKey: TypeAlias = BeliefSource | str
SourceInput: TypeAlias = Mapping[str, object]
SourceInputs: TypeAlias = Mapping[SourceKey, SourceInput]


class BeliefAnalyzer(Protocol):
    def analyze(self, pair: str, **kwargs: object) -> BeliefSnapshot | None:
        """Return a snapshot for one pair or None when analysis fails."""


class BeliefOrchestratorError(ValueError):
    """Base exception for belief orchestrator configuration errors."""


class InvalidBeliefSourceError(BeliefOrchestratorError):
    """Raised when a registered source name is unknown."""

    def __init__(self, source_name: object) -> None:
        self.source_name = source_name
        super().__init__(f"Unsupported belief source registration: {source_name!r}.")


class DuplicateBeliefSourceError(BeliefOrchestratorError):
    """Raised when the same source is registered more than once."""

    def __init__(self, source: BeliefSource) -> None:
        self.source = source
        super().__init__(f"Belief source {source.value!r} was registered more than once.")


class InvalidBeliefAdapterError(BeliefOrchestratorError):
    """Raised when a registered source does not provide a callable analyze method."""

    def __init__(self, source: BeliefSource) -> None:
        self.source = source
        super().__init__(f"Belief source {source.value!r} must expose a callable analyze() method.")


class InvalidStalenessWindowError(BeliefOrchestratorError):
    """Raised when the stale-after duration is not positive."""

    def __init__(self, stale_after: timedelta) -> None:
        self.stale_after = stale_after
        super().__init__(f"stale_after must be positive; got {stale_after}.")


@dataclass(frozen=True, slots=True)
class BeliefCycleResult:
    pair: Pair
    source_beliefs: Mapping[BeliefSource, BeliefSnapshot | None]
    consensus: ConsensusResult
    timestamp: datetime
    stale: bool

    @property
    def is_stale(self) -> bool:
        return self.stale

    @property
    def valid_beliefs(self) -> tuple[BeliefSnapshot, ...]:
        return tuple(
            snapshot
            for snapshot in self.source_beliefs.values()
            if snapshot is not None
        )


class BeliefOrchestrator:
    """Coordinate belief sources for one consensus cycle."""

    def __init__(
        self,
        sources: Mapping[SourceKey, BeliefAnalyzer],
        *,
        stale_after: timedelta = DEFAULT_STALE_AFTER,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if stale_after <= timedelta(0):
            raise InvalidStalenessWindowError(stale_after)

        self._sources = self._normalize_sources(sources)
        self._stale_after = stale_after
        self._clock = clock or _utcnow

    @property
    def registered_sources(self) -> tuple[BeliefSource, ...]:
        return tuple(self._sources.keys())

    def run_belief_cycle(
        self,
        pair: Pair,
        *,
        source_inputs: SourceInputs | None = None,
        timestamp: datetime | None = None,
        reference_time: datetime | None = None,
    ) -> BeliefCycleResult:
        cycle_timestamp = _normalize_timestamp(timestamp or self._clock())
        staleness_reference = _normalize_timestamp(reference_time or cycle_timestamp)
        inputs_by_source = self._normalize_source_inputs(source_inputs)

        source_beliefs: dict[BeliefSource, BeliefSnapshot | None] = {}
        for source_name, source in self._sources.items():
            snapshot = source.analyze(pair, **dict(inputs_by_source.get(source_name, {})))
            source_beliefs[source_name] = snapshot
            if snapshot is None:
                logger.warning(
                    "Belief source %s returned no snapshot for %s.",
                    source_name.value,
                    pair,
                )

        valid_beliefs = tuple(
            snapshot for snapshot in source_beliefs.values() if snapshot is not None
        )
        consensus = (
            compute_consensus(valid_beliefs)
            if len(valid_beliefs) >= 2
            else _inconclusive_consensus(valid_beliefs)
        )

        if len(valid_beliefs) < 2:
            logger.info(
                "Belief cycle for %s is inconclusive: %s valid source(s).",
                pair,
                len(valid_beliefs),
            )

        return BeliefCycleResult(
            pair=pair,
            source_beliefs=MappingProxyType(source_beliefs),
            consensus=consensus,
            timestamp=cycle_timestamp,
            stale=(staleness_reference - cycle_timestamp) >= self._stale_after,
        )

    def _normalize_source_inputs(
        self,
        source_inputs: SourceInputs | None,
    ) -> dict[BeliefSource, SourceInput]:
        if source_inputs is None:
            return {}

        normalized: dict[BeliefSource, SourceInput] = {}
        for source_name, input_values in source_inputs.items():
            normalized[self._coerce_source_name(source_name)] = input_values
        return normalized

    def _normalize_sources(
        self,
        sources: Mapping[SourceKey, BeliefAnalyzer],
    ) -> dict[BeliefSource, BeliefAnalyzer]:
        normalized: dict[BeliefSource, BeliefAnalyzer] = {}
        for source_name, source in sources.items():
            normalized_name = self._coerce_source_name(source_name)
            if normalized_name in normalized:
                raise DuplicateBeliefSourceError(normalized_name)

            analyze = getattr(source, "analyze", None)
            if not callable(analyze):
                raise InvalidBeliefAdapterError(normalized_name)

            normalized[normalized_name] = source
        return normalized

    def _coerce_source_name(self, source_name: SourceKey) -> BeliefSource:
        if isinstance(source_name, BeliefSource):
            return source_name

        try:
            return BeliefSource(str(source_name).lower())
        except ValueError as exc:
            raise InvalidBeliefSourceError(source_name) from exc


def _inconclusive_consensus(
    snapshots: tuple[BeliefSnapshot, ...],
) -> ConsensusResult:
    return ConsensusResult(
        agreed_direction=BeliefDirection.NEUTRAL,
        agreement_count=len(snapshots),
        total_sources=len(snapshots),
        strength_score=0.0,
        regime=MarketRegime.UNKNOWN,
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_timestamp(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


__all__ = [
    "BeliefCycleResult",
    "BeliefOrchestrator",
    "BeliefOrchestratorError",
    "DuplicateBeliefSourceError",
    "InvalidBeliefAdapterError",
    "InvalidBeliefSourceError",
    "InvalidStalenessWindowError",
]
