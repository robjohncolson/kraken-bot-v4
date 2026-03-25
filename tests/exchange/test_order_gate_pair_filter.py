from __future__ import annotations

from decimal import Decimal

import pytest

from core.types import OrderRequest, OrderSide, OrderType
from exchange.client import KrakenClient
from exchange.order_gate import OrderGate, PairNotAllowedError


def _make_order(pair: str = "DOGE/USD") -> OrderRequest:
    return OrderRequest(
        pair=pair,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("50"),
        limit_price=Decimal("0.10"),
    )


def _make_gate(allowed_pairs: frozenset[str] = frozenset()) -> OrderGate:
    return OrderGate(
        client=KrakenClient(api_key="key", api_secret="secret"),
        allowed_pairs=allowed_pairs,
    )


def test_place_order_allowed_pair_passes() -> None:
    gate = _make_gate(frozenset({"DOGE/USD"}))
    request = gate.place_order(_make_order("DOGE/USD"))
    assert request.endpoint == "/0/private/AddOrder"


def test_place_order_disallowed_pair_raises() -> None:
    gate = _make_gate(frozenset({"DOGE/USD"}))
    with pytest.raises(PairNotAllowedError) as exc_info:
        gate.place_order(_make_order("BTC/USD"))
    assert exc_info.value.pair == "BTC/USD"
    assert "DOGE/USD" in str(exc_info.value)


def test_cancel_order_disallowed_pair_raises() -> None:
    gate = _make_gate(frozenset({"DOGE/USD"}))
    with pytest.raises(PairNotAllowedError):
        gate.cancel_order("BTC/USD", "OABC12-XYZZY-123456", order_age_seconds=60.0)


def test_empty_whitelist_allows_all() -> None:
    gate = _make_gate(frozenset())
    request = gate.place_order(_make_order("BTC/USD"))
    assert request.endpoint == "/0/private/AddOrder"


def test_pair_normalization_in_filter() -> None:
    gate = _make_gate(frozenset({"DOGE/USD"}))
    # Kraken-style raw pair should normalize and match
    request = gate.place_order(_make_order("xdgusd"))
    assert request.endpoint == "/0/private/AddOrder"


def test_pair_normalization_disallowed() -> None:
    gate = _make_gate(frozenset({"DOGE/USD"}))
    with pytest.raises(PairNotAllowedError):
        gate.place_order(_make_order("xxbtzusd"))
