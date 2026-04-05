"""Tests for exchange.parsers — Kraken REST response parsing."""

from __future__ import annotations

from decimal import Decimal

import pytest

from exchange.models import KrakenOrder, KrakenTrade
from exchange.parsers import (
    _strip_staking_suffix,
    parse_balances,
    parse_open_orders,
    parse_trade_history,
)


# ── parse_balances ────────────────────────────────────────────────────────


def test_parse_balances_normalizes_assets() -> None:
    result = {"XXDG": "100.5", "ZUSD": "500.25"}
    balances = parse_balances(result)

    assets = {b.asset for b in balances}
    assert assets == {"DOGE", "USD"}

    by_asset = {b.asset: b for b in balances}
    assert by_asset["DOGE"].available == Decimal("100.5")
    assert by_asset["USD"].available == Decimal("500.25")


def test_parse_balances_strips_staking_suffix() -> None:
    result = {"USDT.F": "50.0", "DOT.S": "10.0"}
    balances = parse_balances(result)

    assets = {b.asset for b in balances}
    assert "USDT" in assets
    assert "DOT" in assets


def test_parse_balances_sums_duplicates() -> None:
    result = {"USDT": "100.0", "USDT.F": "50.0"}
    balances = parse_balances(result)

    usdt = [b for b in balances if b.asset == "USDT"]
    assert len(usdt) == 1
    assert usdt[0].available == Decimal("150.0")


def test_parse_balances_filters_zero() -> None:
    result = {"ZUSD": "0.0000", "XXDG": "10.0"}
    balances = parse_balances(result)

    assets = {b.asset for b in balances}
    assert "USD" not in assets
    assert "DOGE" in assets


def test_parse_balances_sorted() -> None:
    result = {"ZUSD": "1.0", "XXDG": "1.0", "XXBT": "1.0"}
    balances = parse_balances(result)

    asset_names = [b.asset for b in balances]
    assert asset_names == sorted(asset_names)


def test_parse_balances_empty() -> None:
    assert parse_balances({}) == ()


# ── parse_open_orders ─────────────────────────────────────────────────────


def test_parse_open_orders_basic() -> None:
    result = {
        "open": {
            "O3KQ7H-BW3P3-BUCMWZ": {
                "descr": {"pair": "XDGUSD"},
                "opentm": 1711296000.0,
                "cl_ord_id": "kbv4-dogeusd-000001",
                "status": "open",
            },
        },
    }
    orders = parse_open_orders(result)

    assert len(orders) == 1
    order = orders[0]
    assert isinstance(order, KrakenOrder)
    assert order.order_id == "O3KQ7H-BW3P3-BUCMWZ"
    assert order.pair == "DOGE/USD"
    assert order.client_order_id == "kbv4-dogeusd-000001"
    assert order.opened_at is not None
    assert order.opened_at.tzname() == "UTC"


def test_parse_open_orders_missing_cl_ord_id() -> None:
    result = {
        "open": {
            "OABCDE-12345-FGHIJK": {
                "descr": {"pair": "XDGUSD"},
                "opentm": 1711296000.0,
                "status": "open",
            },
        },
    }
    orders = parse_open_orders(result)

    assert len(orders) == 1
    assert orders[0].client_order_id is None


def test_parse_open_orders_empty() -> None:
    assert parse_open_orders({"open": {}}) == ()


def test_parse_open_orders_missing_open_key() -> None:
    assert parse_open_orders({}) == ()


# ── parse_trade_history ───────────────────────────────────────────────────


def test_parse_trade_history_basic() -> None:
    result = {
        "trades": {
            "TXXXXX-AAAAA-BBBBBB": {
                "pair": "XDGUSD",
                "ordertxid": "OXXXXX-CCCCC-DDDDDD",
                "type": "buy",
                "vol": "125.5",
                "price": "0.1234",
                "fee": "0.10",
                "time": 1711296000.0,
                "postxid": "PXXXXX-EEEEE-FFFFFF",
                "posstatus": "",
            },
        },
    }
    trades = parse_trade_history(result)

    assert len(trades) == 1
    trade = trades[0]
    assert isinstance(trade, KrakenTrade)
    assert trade.trade_id == "TXXXXX-AAAAA-BBBBBB"
    assert trade.pair == "DOGE/USD"
    assert trade.order_id == "OXXXXX-CCCCC-DDDDDD"
    assert trade.side == "buy"
    assert trade.quantity == Decimal("125.5")
    assert trade.price == Decimal("0.1234")
    assert trade.fee == Decimal("0.10")
    assert trade.filled_at is not None
    assert trade.filled_at.tzname() == "UTC"
    assert trade.position_id == "PXXXXX-EEEEE-FFFFFF"


def test_parse_trade_history_empty_position_id() -> None:
    result = {
        "trades": {
            "TYYYYY-11111-222222": {
                "pair": "XDGUSD",
                "ordertxid": "OYYYYY-33333-444444",
                "type": "sell",
                "vol": "50",
                "price": "0.1250",
                "fee": "0.05",
                "time": 1711296000.0,
                "postxid": "",
                "posstatus": "",
            },
        },
    }
    trades = parse_trade_history(result)

    assert len(trades) == 1
    assert trades[0].position_id is None


def test_parse_trade_history_empty() -> None:
    assert parse_trade_history({"trades": {}}) == ()


def test_parse_trade_history_missing_trades_key() -> None:
    assert parse_trade_history({}) == ()


# ── _strip_staking_suffix ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("USDT.F", "USDT"),
        ("DOT.S", "DOT"),
        ("ETH.M", "ETH"),
        ("BTC", "BTC"),
        ("SOL.P", "SOL"),
    ],
)
def test_strip_staking_suffix(raw: str, expected: str) -> None:
    assert _strip_staking_suffix(raw) == expected
