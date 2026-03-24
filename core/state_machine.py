from __future__ import annotations

from typing import Final, TypeAlias

from core.config import Settings
from core.types import (
    Action,
    BeliefUpdate,
    BotState,
    Event,
    FillConfirmed,
    GridCycleComplete,
    PriceTick,
    ReconciliationResult,
    StopTriggered,
    TargetHit,
)

ReducerActions: TypeAlias = tuple[Action, ...]
ReducerResult: TypeAlias = tuple[BotState, ReducerActions]

NO_ACTIONS: Final[ReducerActions] = ()


class UnsupportedEventError(TypeError):
    """Raised when reduce receives an event outside the declared Event union."""

    def __init__(self, event_name: str) -> None:
        self.event_name = event_name
        super().__init__(f"Unsupported reducer event: {event_name}")


def reduce(state: BotState, event: Event, config: Settings) -> ReducerResult:
    """Return a deterministic no-op transition until reducer rules are implemented."""

    _ = config
    match event:
        case PriceTick():
            return _noop_transition(state)
        case FillConfirmed():
            return _noop_transition(state)
        case StopTriggered():
            return _noop_transition(state)
        case TargetHit():
            return _noop_transition(state)
        case BeliefUpdate():
            return _noop_transition(state)
        case ReconciliationResult():
            return _noop_transition(state)
        case GridCycleComplete():
            return _noop_transition(state)
        case _:
            raise UnsupportedEventError(type(event).__name__)


def _noop_transition(state: BotState) -> ReducerResult:
    return state, NO_ACTIONS


__all__ = [
    "NO_ACTIONS",
    "ReducerActions",
    "ReducerResult",
    "UnsupportedEventError",
    "reduce",
]
