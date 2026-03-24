from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.errors import KrakenBotError
from core.types import UsdAmount, ZERO_DECIMAL
from grid.states import GridCycleRecord


class GridAccountingError(KrakenBotError):
    """Base exception for grid accounting failures."""


class GridCycleAccountingMismatchError(GridAccountingError):
    """Raised when a cycle record's net P&L does not match gross minus fees."""

    def __init__(self, cycle: GridCycleRecord) -> None:
        self.cycle = cycle
        super().__init__(
            "Grid cycle record is internally inconsistent: "
            f"net_pnl_usd={cycle.net_pnl_usd} but gross_pnl_usd - fees_usd="
            f"{cycle.gross_pnl_usd - cycle.fees_usd}."
        )


class InvalidParentRiskAmountError(GridAccountingError):
    """Raised when parent-position risk is zero or negative."""

    def __init__(self, parent_position_risk_usd: UsdAmount) -> None:
        self.parent_position_risk_usd = parent_position_risk_usd
        super().__init__(
            "Parent position risk must be positive to calculate a grid risk adjustment; "
            f"got {parent_position_risk_usd}."
        )


@dataclass(frozen=True, slots=True)
class GridAccountingSummary:
    """Cumulative realized grid accounting for one parent position."""

    total_cycles: int = 0
    total_profit_usd: UsdAmount = ZERO_DECIMAL
    total_fees_usd: UsdAmount = ZERO_DECIMAL
    net_pnl_usd: UsdAmount = ZERO_DECIMAL


def book_cycle(
    summary: GridAccountingSummary,
    cycle: GridCycleRecord,
) -> GridAccountingSummary:
    """Return a new summary with one completed cycle booked into cumulative totals."""

    _validate_cycle_record(cycle)
    return GridAccountingSummary(
        total_cycles=summary.total_cycles + 1,
        total_profit_usd=summary.total_profit_usd + cycle.gross_pnl_usd,
        total_fees_usd=summary.total_fees_usd + cycle.fees_usd,
        net_pnl_usd=summary.net_pnl_usd + cycle.net_pnl_usd,
    )


def risk_adjustment(
    summary: GridAccountingSummary,
    parent_position_risk_usd: UsdAmount,
) -> Decimal:
    """Return the risk-score delta implied by cumulative grid P&L.

    Positive grid P&L reduces parent-position risk, so the adjustment is negative.
    Negative grid P&L increases parent-position risk, so the adjustment is positive.
    """

    if parent_position_risk_usd <= ZERO_DECIMAL:
        raise InvalidParentRiskAmountError(parent_position_risk_usd)
    return -summary.net_pnl_usd / parent_position_risk_usd


def _validate_cycle_record(cycle: GridCycleRecord) -> None:
    if cycle.net_pnl_usd != cycle.gross_pnl_usd - cycle.fees_usd:
        raise GridCycleAccountingMismatchError(cycle)


__all__ = [
    "book_cycle",
    "GridAccountingError",
    "GridAccountingSummary",
    "GridCycleAccountingMismatchError",
    "InvalidParentRiskAmountError",
    "risk_adjustment",
]
