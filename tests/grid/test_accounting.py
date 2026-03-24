from __future__ import annotations

from decimal import Decimal

import pytest

from core.types import OrderSide
from grid.accounting import (
    GridAccountingSummary,
    GridCycleAccountingMismatchError,
    InvalidParentRiskAmountError,
    book_cycle,
    risk_adjustment,
)
from grid.states import GridCycleRecord


def _cycle(
    *,
    gross_pnl_usd: Decimal,
    fees_usd: Decimal,
) -> GridCycleRecord:
    return GridCycleRecord(
        pair="DOGE/USD",
        entry_side=OrderSide.BUY,
        quantity=Decimal("10"),
        entry_price=Decimal("0.80"),
        exit_price=Decimal("1.00"),
        gross_pnl_usd=gross_pnl_usd,
        fees_usd=fees_usd,
        net_pnl_usd=gross_pnl_usd - fees_usd,
    )


def test_book_cycle_tracks_profitable_cycle_and_returns_new_summary() -> None:
    summary = GridAccountingSummary()

    updated = book_cycle(summary, _cycle(gross_pnl_usd=Decimal("2.00"), fees_usd=Decimal("0.10")))

    assert updated is not summary
    assert summary == GridAccountingSummary()
    assert updated == GridAccountingSummary(
        total_cycles=1,
        total_profit_usd=Decimal("2.00"),
        total_fees_usd=Decimal("0.10"),
        net_pnl_usd=Decimal("1.90"),
    )


def test_book_cycle_accumulates_losing_cycle_into_existing_totals() -> None:
    summary = GridAccountingSummary(
        total_cycles=1,
        total_profit_usd=Decimal("2.00"),
        total_fees_usd=Decimal("0.10"),
        net_pnl_usd=Decimal("1.90"),
    )

    updated = book_cycle(summary, _cycle(gross_pnl_usd=Decimal("-1.50"), fees_usd=Decimal("0.25")))

    assert updated == GridAccountingSummary(
        total_cycles=2,
        total_profit_usd=Decimal("0.50"),
        total_fees_usd=Decimal("0.35"),
        net_pnl_usd=Decimal("0.15"),
    )


def test_book_cycle_deducts_fees_from_net_pnl() -> None:
    summary = GridAccountingSummary()

    updated = book_cycle(summary, _cycle(gross_pnl_usd=Decimal("0.75"), fees_usd=Decimal("0.20")))

    assert updated.total_profit_usd == Decimal("0.75")
    assert updated.total_fees_usd == Decimal("0.20")
    assert updated.net_pnl_usd == Decimal("0.55")


@pytest.mark.parametrize(
    ("net_pnl_usd", "parent_position_risk_usd", "expected"),
    [
        (Decimal("6.00"), Decimal("24.00"), Decimal("-0.25")),
        (Decimal("-6.00"), Decimal("24.00"), Decimal("0.25")),
        (Decimal("0"), Decimal("24.00"), Decimal("0")),
    ],
)
def test_risk_adjustment_scales_net_grid_pnl_against_parent_risk(
    net_pnl_usd: Decimal,
    parent_position_risk_usd: Decimal,
    expected: Decimal,
) -> None:
    summary = GridAccountingSummary(net_pnl_usd=net_pnl_usd)

    assert risk_adjustment(summary, parent_position_risk_usd) == expected


def test_book_cycle_rejects_inconsistent_cycle_records() -> None:
    cycle = GridCycleRecord(
        pair="DOGE/USD",
        entry_side=OrderSide.SELL,
        quantity=Decimal("5"),
        entry_price=Decimal("1.20"),
        exit_price=Decimal("1.00"),
        gross_pnl_usd=Decimal("1.00"),
        fees_usd=Decimal("0.10"),
        net_pnl_usd=Decimal("0.95"),
    )

    with pytest.raises(GridCycleAccountingMismatchError):
        book_cycle(GridAccountingSummary(), cycle)


def test_risk_adjustment_rejects_non_positive_parent_risk() -> None:
    with pytest.raises(InvalidParentRiskAmountError):
        risk_adjustment(
            GridAccountingSummary(net_pnl_usd=Decimal("1.00")),
            Decimal("0"),
        )
