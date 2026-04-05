from __future__ import annotations

import base64
import json
from decimal import Decimal
from io import BytesIO
from urllib.request import Request

from exchange.client import KrakenClient, KrakenRateLimiter
from exchange.executor import KrakenExecutor
from exchange.models import KrakenState
from exchange.transport import HttpKrakenTransport, RetryPolicy


def _make_sender(result: object) -> tuple[list[Request], object]:
    """Return (call_log, sender) where sender returns a mock HTTP response."""
    calls: list[Request] = []

    def sender(req: Request, timeout: float) -> BytesIO:
        calls.append(req)
        body = json.dumps({"error": [], "result": result}).encode()
        resp = BytesIO(body)
        resp.read = resp.read  # noqa: PLW0127
        return resp  # type: ignore[return-value]

    return calls, sender


def _make_multi_sender(results: list[object]) -> tuple[list[Request], object]:
    """Return a sender that yields a different result per call."""
    calls: list[Request] = []
    index = {"i": 0}

    def sender(req: Request, timeout: float) -> BytesIO:
        calls.append(req)
        result = results[index["i"]]
        index["i"] += 1
        body = json.dumps({"error": [], "result": result}).encode()
        resp = BytesIO(body)
        return resp  # type: ignore[return-value]

    return calls, sender


def _executor(sender: object) -> KrakenExecutor:
    secret = base64.b64encode(b"test_secret").decode()
    client = KrakenClient(
        api_key="test_key",
        api_secret=secret,
        rate_limiter=KrakenRateLimiter(now=lambda: 0.0),
    )
    transport = HttpKrakenTransport(
        retry_policy=RetryPolicy(max_retries=0),
        sender=sender,  # type: ignore[arg-type]
    )
    return KrakenExecutor(
        client=client,
        transport=transport,
        nonce_source=lambda: 1000,
    )


def test_fetch_balances_returns_parsed_balances() -> None:
    _, sender = _make_sender({"XXDG": "100.5", "ZUSD": "500.25"})
    executor = _executor(sender)

    balances = executor.fetch_balances()

    assert len(balances) == 2
    assets = {b.asset for b in balances}
    assert assets == {"DOGE", "USD"}
    doge = next(b for b in balances if b.asset == "DOGE")
    assert doge.available == Decimal("100.5")


def test_fetch_open_orders_returns_parsed_orders() -> None:
    _, sender = _make_sender(
        {
            "open": {
                "O3KQ7H-AAAA-BBBBBB": {
                    "descr": {"pair": "XDGUSD"},
                    "opentm": 1711296000.0,
                    "cl_ord_id": "kbv4-dogeusd-000001",
                    "status": "open",
                }
            }
        }
    )
    executor = _executor(sender)

    orders = executor.fetch_open_orders()

    assert len(orders) == 1
    assert orders[0].order_id == "O3KQ7H-AAAA-BBBBBB"
    assert orders[0].pair == "DOGE/USD"
    assert orders[0].client_order_id == "kbv4-dogeusd-000001"


def test_fetch_trade_history_returns_parsed_trades() -> None:
    _, sender = _make_sender(
        {
            "trades": {
                "T1234-ABCD-EFGH": {
                    "pair": "XDGUSD",
                    "ordertxid": "O9999-XXXX-YYYY",
                    "type": "buy",
                    "vol": "125",
                    "price": "0.1234",
                    "fee": "0.10",
                    "time": 1711296000.0,
                    "postxid": "",
                }
            }
        }
    )
    executor = _executor(sender)

    trades = executor.fetch_trade_history()

    assert len(trades) == 1
    assert trades[0].trade_id == "T1234-ABCD-EFGH"
    assert trades[0].quantity == Decimal("125")
    assert trades[0].price == Decimal("0.1234")
    assert trades[0].fee == Decimal("0.10")
    assert trades[0].position_id is None


def test_fetch_kraken_state_assembles_all_three() -> None:
    results = [
        {"ZUSD": "100.0"},
        {"open": {}},
        {"trades": {}},
    ]
    _, sender = _make_multi_sender(results)
    executor = _executor(sender)

    state = executor.fetch_kraken_state()

    assert isinstance(state, KrakenState)
    assert len(state.balances) == 1
    assert state.balances[0].asset == "USD"
    assert state.open_orders == ()
    assert state.trade_history == ()


def test_fetch_kraken_state_sends_three_requests() -> None:
    results = [
        {"ZUSD": "1.0"},
        {"open": {}},
        {"trades": {}},
    ]
    calls, sender = _make_multi_sender(results)
    executor = _executor(sender)

    executor.fetch_kraken_state()

    assert len(calls) == 3
    urls = [c.full_url for c in calls]
    assert any("Balance" in u for u in urls)
    assert any("OpenOrders" in u for u in urls)
    assert any("TradesHistory" in u for u in urls)


def test_executor_passes_api_credentials_to_signing() -> None:
    calls, sender = _make_sender({"ZUSD": "1.0"})
    executor = _executor(sender)

    executor.fetch_balances()

    assert len(calls) == 1
    assert calls[0].get_header("Api-key") == "test_key"
    assert calls[0].get_header("Api-sign") is not None
