from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal as _Decimal
from typing import TYPE_CHECKING, TypeAlias

from core.config import Settings
from core.errors import KrakenBotError
from core.state_machine import reduce as default_reduce
from core.types import (
    Action,
    BeliefSnapshot,
    BeliefUpdate,
    BotState,
    FillConfirmed,
    GridCycleComplete,
    ReconciliationResult,
    StopTriggered,
    TargetHit,
    WindowExpired,
)
from guardian import CurrentPrices, Guardian, GuardianAction, GuardianActionType, PriceSnapshot
from trading.portfolio import mark_to_market
from trading.reconciler import KrakenState, ReconciliationReport, RecordedState, reconcile

if TYPE_CHECKING:
    from trading.conditional_tree import ConditionalTreeState

Reducer: TypeAlias = Callable[[BotState, object, Settings], tuple[BotState, tuple[Action, ...]]]
Reconciler: TypeAlias = Callable[..., ReconciliationReport]


class SchedulerError(KrakenBotError):
    """Base exception for scheduler orchestration errors."""


class InvalidSchedulerIntervalError(SchedulerError):
    """Raised when a scheduler interval is not positive."""

    def __init__(self, field_name: str, raw_value: int) -> None:
        self.field_name = field_name
        self.raw_value = raw_value
        super().__init__(f"{field_name} must be positive; got {raw_value}.")


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    cycle_interval_sec: int
    reconcile_interval_sec: int
    guardian_interval_sec: int

    def __post_init__(self) -> None:
        _require_positive_interval("cycle_interval_sec", self.cycle_interval_sec)
        _require_positive_interval("reconcile_interval_sec", self.reconcile_interval_sec)
        _require_positive_interval("guardian_interval_sec", self.guardian_interval_sec)


@dataclass(frozen=True, slots=True)
class BeliefRefreshRequest:
    pair: str
    position_id: str
    checked_at: datetime
    stale_after_hours: int


@dataclass(frozen=True, slots=True)
class ReconciliationDiscrepancy:
    report: ReconciliationReport
    summary: str


@dataclass(frozen=True, slots=True)
class DashboardStateUpdate:
    bot_state: BotState
    cycle_count: int
    emitted_at: datetime
    next_cycle_due_at: datetime


SchedulerEffect: TypeAlias = (
    Action
    | GuardianAction
    | BeliefRefreshRequest
    | ReconciliationDiscrepancy
    | DashboardStateUpdate
)


@dataclass(frozen=True, slots=True)
class SchedulerState:
    bot_state: BotState = field(default_factory=BotState)
    current_prices: CurrentPrices = field(default_factory=dict)
    kraken_state: KrakenState = field(default_factory=KrakenState)
    recorded_state: RecordedState = field(default_factory=RecordedState)
    conditional_tree_state: ConditionalTreeState | None = None
    pending_belief_signals: tuple[BeliefSnapshot, ...] = ()
    pending_fills: tuple[FillConfirmed, ...] = ()
    pending_grid_cycles: tuple[GridCycleComplete, ...] = ()
    now: datetime = field(default_factory=datetime.utcnow)
    last_cycle_at: datetime | None = None
    last_guardian_check_at: datetime | None = None
    last_reconcile_at: datetime | None = None
    last_reconciliation_report: ReconciliationReport | None = None
    cycle_count: int = 0


class Scheduler:
    """Pure main-loop orchestrator for one scheduler iteration."""

    def __init__(
        self,
        *,
        config: SchedulerConfig,
        settings: Settings,
        guardian: Guardian | None = None,
        reducer: Reducer = default_reduce,
        reconciler: Reconciler = reconcile,
    ) -> None:
        self._config = config
        self._settings = settings
        self._guardian = guardian or Guardian()
        self._reducer = reducer
        self._reconciler = reconciler

    def run_cycle(
        self,
        state: SchedulerState,
    ) -> tuple[SchedulerState, tuple[SchedulerEffect, ...]]:
        working_state = state
        effects: list[SchedulerEffect] = []

        guardian_actions: tuple[GuardianAction, ...] = ()
        if _interval_due(
            working_state.last_guardian_check_at,
            working_state.now,
            self._config.guardian_interval_sec,
        ):
            guardian_actions = tuple(
                self._guardian.check_positions(
                    working_state.current_prices,
                    working_state.bot_state.portfolio,
                    self._settings,
                    conditional_tree_state=working_state.conditional_tree_state,
                    as_of=working_state.now,
                )
            )
            working_state = replace(working_state, last_guardian_check_at=working_state.now)
            effects.extend(guardian_actions)
            working_state, reducer_actions = self._apply_guardian_events(
                working_state,
                guardian_actions,
            )
            effects.extend(reducer_actions)

        effects.extend(_belief_staleness_actions(guardian_actions))

        if _interval_due(
            working_state.last_reconcile_at,
            working_state.now,
            self._config.reconcile_interval_sec,
        ):
            report = self._reconciler(
                working_state.kraken_state,
                working_state.recorded_state,
                as_of=working_state.now,
            )
            summary = _reconciliation_summary(report)
            working_state = replace(
                working_state,
                last_reconcile_at=working_state.now,
                last_reconciliation_report=report,
            )
            working_state, reducer_actions = self._apply_event(
                working_state,
                ReconciliationResult(
                    balances=working_state.kraken_state.balances,
                    open_orders=working_state.bot_state.open_orders,
                    discrepancy_detected=report.discrepancy_detected,
                    summary=summary,
                ),
            )
            effects.extend(reducer_actions)
            if report.discrepancy_detected:
                effects.append(ReconciliationDiscrepancy(report=report, summary=summary))

        working_state, fill_actions = self._process_fills(working_state)
        effects.extend(fill_actions)

        working_state, belief_actions = self._process_belief_signals(working_state)
        effects.extend(belief_actions)

        working_state, grid_actions = self._process_grid_cycles(working_state)
        effects.extend(grid_actions)

        cycle_count = working_state.cycle_count + 1
        working_state = replace(
            working_state,
            cycle_count=cycle_count,
            last_cycle_at=working_state.now,
        )
        effects.append(
            DashboardStateUpdate(
                bot_state=working_state.bot_state,
                cycle_count=cycle_count,
                emitted_at=working_state.now,
                next_cycle_due_at=working_state.now
                + timedelta(seconds=self._config.cycle_interval_sec),
            )
        )
        return working_state, tuple(effects)

    def _apply_guardian_events(
        self,
        state: SchedulerState,
        guardian_actions: tuple[GuardianAction, ...],
    ) -> tuple[SchedulerState, tuple[Action, ...]]:
        reducer_actions: list[Action] = []
        working_state = state

        for guardian_action in guardian_actions:
            event = _guardian_event(guardian_action)
            if event is None:
                continue
            working_state, applied_actions = self._apply_event(working_state, event)
            reducer_actions.extend(applied_actions)

        return working_state, tuple(reducer_actions)

    def _process_fills(
        self,
        state: SchedulerState,
    ) -> tuple[SchedulerState, tuple[Action, ...]]:
        reducer_actions: list[Action] = []
        working_state = state

        for fill in working_state.pending_fills:
            working_state, applied_actions = self._apply_event(working_state, fill)
            reducer_actions.extend(applied_actions)

        return replace(working_state, pending_fills=()), tuple(reducer_actions)

    def _process_belief_signals(
        self,
        state: SchedulerState,
    ) -> tuple[SchedulerState, tuple[Action, ...]]:
        reducer_actions: list[Action] = []
        working_state = state

        for belief in working_state.pending_belief_signals:
            working_state, applied_actions = self._apply_event(
                working_state,
                BeliefUpdate(belief=belief),
            )
            reducer_actions.extend(applied_actions)

        return replace(working_state, pending_belief_signals=()), tuple(reducer_actions)

    def _process_grid_cycles(
        self,
        state: SchedulerState,
    ) -> tuple[SchedulerState, tuple[Action, ...]]:
        reducer_actions: list[Action] = []
        working_state = state

        for grid_cycle in working_state.pending_grid_cycles:
            working_state, applied_actions = self._apply_event(working_state, grid_cycle)
            reducer_actions.extend(applied_actions)

        return replace(working_state, pending_grid_cycles=()), tuple(reducer_actions)

    def _apply_event(
        self,
        state: SchedulerState,
        event: object,
    ) -> tuple[SchedulerState, tuple[Action, ...]]:
        prices = _extract_reference_prices(state.current_prices)
        bot_state_with_time = replace(
            state.bot_state, as_of=state.now, reference_prices=prices,
        )
        reduced_bot_state, reducer_actions = self._reducer(
            bot_state_with_time,
            event,
            self._settings,
        )
        merged_bot_state = _merge_event_state(reduced_bot_state, event)
        doge_price = _extract_doge_price(state.current_prices)
        merged_bot_state = replace(
            merged_bot_state,
            portfolio=mark_to_market(merged_bot_state.portfolio, doge_price_usd=doge_price),
        )
        next_state = replace(state, bot_state=merged_bot_state)
        if isinstance(event, WindowExpired):
            next_state = replace(next_state, conditional_tree_state=None)
        elif isinstance(event, (StopTriggered, TargetHit)):
            tree = next_state.conditional_tree_state
            if tree is not None and tree.position_id == event.position_id:
                next_state = replace(next_state, conditional_tree_state=None)
        return next_state, reducer_actions


def _require_positive_interval(field_name: str, raw_value: int) -> None:
    if raw_value <= 0:
        raise InvalidSchedulerIntervalError(field_name, raw_value)


def _interval_due(last_run_at: datetime | None, now: datetime, interval_seconds: int) -> bool:
    if last_run_at is None:
        return True
    return now - last_run_at >= timedelta(seconds=interval_seconds)


def _belief_staleness_actions(
    guardian_actions: tuple[GuardianAction, ...],
) -> tuple[BeliefRefreshRequest, ...]:
    refresh_requests: list[BeliefRefreshRequest] = []
    for guardian_action in guardian_actions:
        if guardian_action.action_type != GuardianActionType.BELIEF_STALE:
            continue
        refresh_requests.append(
            BeliefRefreshRequest(
                pair=str(guardian_action.details["pair"]),
                position_id=str(guardian_action.details["position_id"]),
                checked_at=guardian_action.details["checked_at"],  # type: ignore[arg-type]
                stale_after_hours=int(guardian_action.details["stale_after_hours"]),
            )
        )
    return tuple(refresh_requests)


def _guardian_event(guardian_action: GuardianAction) -> StopTriggered | TargetHit | WindowExpired | None:
    if guardian_action.action_type == GuardianActionType.STOP_TRIGGERED:
        return StopTriggered(
            position_id=str(guardian_action.details["position_id"]),
            trigger_price=guardian_action.details["trigger_price"],  # type: ignore[arg-type]
        )
    if guardian_action.action_type == GuardianActionType.TARGET_HIT:
        return TargetHit(
            position_id=str(guardian_action.details["position_id"]),
            trigger_price=guardian_action.details["trigger_price"],  # type: ignore[arg-type]
        )
    if guardian_action.action_type == GuardianActionType.WINDOW_EXPIRED:
        return WindowExpired(
            pair=str(guardian_action.details["pair"]),
            position_id=guardian_action.details["position_id"],  # type: ignore[arg-type]
            trigger_price=guardian_action.details["trigger_price"],  # type: ignore[arg-type]
            expired_at=guardian_action.details["expired_at"],  # type: ignore[arg-type]
        )
    return None


def _merge_event_state(bot_state: BotState, event: object) -> BotState:
    merged_state = bot_state
    if isinstance(event, BeliefUpdate):
        merged_state = replace(merged_state, beliefs=_upsert_belief(merged_state.beliefs, event.belief))
    elif isinstance(event, ReconciliationResult):
        merged_state = replace(
            merged_state,
            balances=event.balances,
            open_orders=event.open_orders,
        )
    return replace(merged_state, last_event=event.kind)  # type: ignore[attr-defined]


def _upsert_belief(
    beliefs: tuple[BeliefSnapshot, ...],
    new_belief: BeliefSnapshot,
) -> tuple[BeliefSnapshot, ...]:
    updated = [belief for belief in beliefs if belief.pair != new_belief.pair]
    updated.append(new_belief)
    return tuple(sorted(updated, key=lambda belief: belief.pair))


def _extract_reference_prices(
    current_prices: CurrentPrices,
) -> tuple[tuple[str, _Decimal], ...]:
    result: list[tuple[str, _Decimal]] = []
    for pair, snap in current_prices.items():
        price = snap.price if isinstance(snap, PriceSnapshot) else snap
        if isinstance(price, _Decimal):
            result.append((pair, price))
    return tuple(result)


def _extract_doge_price(current_prices: CurrentPrices) -> _Decimal:
    for pair, snap in current_prices.items():
        if pair == "DOGE/USD":
            price = snap.price if isinstance(snap, PriceSnapshot) else snap
            return price if isinstance(price, _Decimal) else _Decimal(0)
    return _Decimal(0)


def _reconciliation_summary(report: ReconciliationReport) -> str:
    return (
        f"ghost_positions={len(report.ghost_positions)} "
        f"foreign_orders={len(report.foreign_orders)} "
        f"fee_drift={len(report.fee_drift)} "
        f"untracked_assets={len(report.untracked_assets)}"
    )


__all__ = [
    "BeliefRefreshRequest",
    "DashboardStateUpdate",
    "InvalidSchedulerIntervalError",
    "ReconciliationDiscrepancy",
    "Scheduler",
    "SchedulerConfig",
    "SchedulerEffect",
    "SchedulerError",
    "SchedulerState",
]
