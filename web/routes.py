from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder

from core.types import (
    BeliefDirection,
    BeliefSource,
    GridPhase,
    MarketRegime,
    Pair,
    Portfolio,
    Position,
    ZERO_DECIMAL,
)
from trading.reconciler import ReconciliationReport


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    position: Position
    current_price: Decimal
    unrealized_pnl_usd: Decimal = ZERO_DECIMAL


@dataclass(frozen=True, slots=True)
class GridPhaseCount:
    phase: GridPhase
    active_slots: int


@dataclass(frozen=True, slots=True)
class GridCycleSnapshot:
    cycle_id: str
    realized_pnl_usd: Decimal = ZERO_DECIMAL
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class GridStatusSnapshot:
    pair: Pair
    active_slots: int
    phase_distribution: tuple[GridPhaseCount, ...] = field(default_factory=tuple)
    cycle_history: tuple[GridCycleSnapshot, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BeliefEntry:
    pair: Pair
    source: BeliefSource
    direction: BeliefDirection
    confidence: float
    regime: MarketRegime = MarketRegime.UNKNOWN
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class StrategyStatsSnapshot:
    trade_count: int = 0
    win_rate: float | None = None
    win_rate_ci_low: float | None = None
    win_rate_ci_high: float | None = None
    sharpe_ratio: float | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationSnapshot:
    checked_at: datetime | None = None
    report: ReconciliationReport | None = None


@dataclass(frozen=True, slots=True)
class DashboardState:
    portfolio: Portfolio = field(default_factory=Portfolio)
    positions: tuple[PositionSnapshot, ...] = field(default_factory=tuple)
    grids: tuple[GridStatusSnapshot, ...] = field(default_factory=tuple)
    beliefs: tuple[BeliefEntry, ...] = field(default_factory=tuple)
    stats: StrategyStatsSnapshot = field(default_factory=StrategyStatsSnapshot)
    reconciliation: ReconciliationSnapshot = field(default_factory=ReconciliationSnapshot)


class DashboardStateProvider(Protocol):
    def __call__(self) -> DashboardState: ...


def create_router(*, state_provider: DashboardStateProvider) -> APIRouter:
    router = APIRouter(prefix="/api")

    def get_state() -> DashboardState:
        return state_provider()

    @router.get("/portfolio")
    def read_portfolio(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _encode_payload(state.portfolio)

    @router.get("/positions")
    def read_positions(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return {"positions": _encode_payload(state.positions)}

    @router.get("/grid/{pair:path}")
    def read_grid(pair: str, state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        for grid in state.grids:
            if grid.pair == pair:
                return _serialize_grid_status(grid)
        raise HTTPException(status_code=404, detail=f"Grid state not found for pair {pair!r}.")

    @router.get("/beliefs")
    def read_beliefs(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return {"beliefs": _serialize_beliefs(state.beliefs)}

    @router.get("/stats")
    def read_stats(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _encode_payload(state.stats)

    @router.get("/reconciliation")
    def read_reconciliation(state: DashboardState = Depends(get_state)) -> dict[str, Any]:
        return _serialize_reconciliation(state.reconciliation)

    return router


def _serialize_grid_status(grid: GridStatusSnapshot) -> dict[str, Any]:
    return {
        "pair": grid.pair,
        "active_slots": grid.active_slots,
        "phase_distribution": {
            item.phase.value: item.active_slots for item in grid.phase_distribution
        },
        "cycle_history": _encode_payload(grid.cycle_history),
    }


def _serialize_beliefs(beliefs: tuple[BeliefEntry, ...]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for belief in beliefs:
        payload = _encode_payload(belief)
        pair = payload.pop("pair")
        source = payload.pop("source")
        grouped.setdefault(pair, {})[source] = payload
    return grouped


def _serialize_reconciliation(snapshot: ReconciliationSnapshot) -> dict[str, Any]:
    report = snapshot.report
    payload: dict[str, Any] = {
        "checked_at": _encode_payload(snapshot.checked_at),
        "discrepancy_detected": False if report is None else report.discrepancy_detected,
        "ghost_positions": [],
        "foreign_orders": [],
        "fee_drift": [],
        "untracked_assets": [],
    }
    report_payload = _encode_payload(report)
    if isinstance(report_payload, dict):
        payload.update(report_payload)
    return payload


def _encode_payload(payload: object) -> Any:
    return jsonable_encoder(payload)


__all__ = [
    "BeliefEntry",
    "DashboardState",
    "DashboardStateProvider",
    "GridCycleSnapshot",
    "GridPhaseCount",
    "GridStatusSnapshot",
    "PositionSnapshot",
    "ReconciliationSnapshot",
    "StrategyStatsSnapshot",
    "create_router",
]
