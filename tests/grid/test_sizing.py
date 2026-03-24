from __future__ import annotations

from decimal import Decimal

import pytest

from grid.sizing import (
    SlotAllocation,
    UnknownPairMinimumError,
    calculate_slot_count,
    min_slot_size_usd,
)


@pytest.mark.parametrize(
    ("pair", "reference_price", "expected"),
    [
        ("DOGE/USD", Decimal("0.20"), Decimal("10.00")),
        ("BTC/USD", Decimal("80000"), Decimal("4.00000")),
        ("XRP/USD", Decimal("0.55"), Decimal("5.50")),
    ],
)
def test_min_slot_size_usd_uses_pair_specific_kraken_minimums(
    pair: str,
    reference_price: Decimal,
    expected: Decimal,
) -> None:
    assert min_slot_size_usd(pair, reference_price) == expected


def test_calculate_slot_count_uses_maximum_number_of_minimum_sized_slots() -> None:
    allocation = calculate_slot_count(
        available_capital_usd=Decimal("57"),
        pair="DOGE/USD",
        reference_price=Decimal("0.20"),
    )

    assert allocation == SlotAllocation(
        pair="DOGE/USD",
        available_capital_usd=Decimal("57"),
        minimum_quantity=Decimal("50"),
        minimum_slot_size_usd=Decimal("10.00"),
        slot_count=5,
        allocated_capital_usd=Decimal("50.00"),
        remainder_usd=Decimal("7.00"),
    )


def test_calculate_slot_count_reports_remainder_when_capital_is_below_minimum() -> None:
    allocation = calculate_slot_count(
        available_capital_usd=Decimal("9.99"),
        pair="DOGE/USD",
        reference_price=Decimal("0.20"),
    )

    assert allocation.slot_count == 0
    assert allocation.minimum_slot_size_usd == Decimal("10.00")
    assert allocation.allocated_capital_usd == Decimal("0.00")
    assert allocation.remainder_usd == Decimal("9.99")


def test_min_slot_size_usd_rejects_unknown_pairs() -> None:
    with pytest.raises(UnknownPairMinimumError):
        min_slot_size_usd("UNKNOWN/USD", Decimal("1"))
