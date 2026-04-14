from __future__ import annotations

from decimal import Decimal

from core.types import Portfolio


def test_portfolio_total_value_defaults_to_at_least_cash() -> None:
    portfolio = Portfolio(cash_usd=Decimal("100"))

    assert portfolio.total_value_usd >= Decimal("100")
