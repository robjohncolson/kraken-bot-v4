from __future__ import annotations

from collections.abc import Callable

import pytest

from core.errors import (
    ExchangeError,
    InsufficientFundsError,
    KrakenAPIError,
    OrderRejectedError,
    RateLimitExceededError,
    SafeModeBlockedError,
)
from exchange.parsers import (
    KrakenResponseError,
    parse_add_order_response,
    parse_cancel_order_response,
)

MutationParser = Callable[[dict[str, object]], object]


def test_parse_add_order_response_returns_txids() -> None:
    response = {
        "error": [],
        "result": {
            "descr": {"order": "buy 1 DOGEUSD @ limit 0.10"},
            "txid": ["OUF4EM-FRGI2-MQMWZD", "OUF4EM-FRGI2-MQMWZE"],
        },
    }

    assert parse_add_order_response(response) == (
        "OUF4EM-FRGI2-MQMWZD",
        "OUF4EM-FRGI2-MQMWZE",
    )


def test_parse_cancel_order_response_returns_count() -> None:
    response = {"error": [], "result": {"count": 1, "pending": 0}}

    assert parse_cancel_order_response(response) == 1


@pytest.mark.parametrize(
    ("parser", "response", "error_type", "match"),
    [
        (
            parse_add_order_response,
            {"error": ["EOrder:Insufficient funds"], "result": None},
            InsufficientFundsError,
            "Insufficient funds",
        ),
        (
            parse_add_order_response,
            {"error": ["EOrder:Orders limit exceeded"], "result": None},
            OrderRejectedError,
            "Orders limit exceeded",
        ),
        (
            parse_add_order_response,
            {"error": ["EAPI:Rate limit exceeded"], "result": None},
            RateLimitExceededError,
            "Rate limit exceeded",
        ),
        (
            parse_add_order_response,
            {"error": ["EGeneral:Permission denied"], "result": None},
            KrakenAPIError,
            "Permission denied",
        ),
        (
            parse_cancel_order_response,
            {"error": ["EOrder:Unknown order"], "result": None},
            OrderRejectedError,
            "Unknown order",
        ),
    ],
)
def test_mutation_parsers_raise_typed_kraken_errors(
    parser: MutationParser,
    response: dict[str, object],
    error_type: type[Exception],
    match: str,
) -> None:
    with pytest.raises(error_type, match=match):
        parser(response)


@pytest.mark.parametrize(
    ("parser", "response", "match"),
    [
        (
            parse_add_order_response,
            {"error": [], "result": {}},
            "txid",
        ),
        (
            parse_cancel_order_response,
            {"error": [], "result": {}},
            "count",
        ),
    ],
)
def test_mutation_parsers_reject_malformed_results(
    parser: MutationParser,
    response: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(KrakenResponseError, match=match):
        parser(response)


@pytest.mark.parametrize(
    ("parser", "response", "match"),
    [
        (
            parse_add_order_response,
            {"error": "EOrder:Insufficient funds", "result": {}},
            "Invalid 'error' list",
        ),
        (
            parse_cancel_order_response,
            {"error": ["EOrder:Unknown order", 1], "result": {}},
            "Invalid 'error' list",
        ),
        (
            parse_add_order_response,
            {"error": []},
            "Missing or invalid 'result' mapping",
        ),
        (
            parse_cancel_order_response,
            {"error": [], "result": []},
            "Missing or invalid 'result' mapping",
        ),
    ],
)
def test_mutation_parsers_reject_invalid_response_envelopes(
    parser: MutationParser,
    response: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(KrakenResponseError, match=match):
        parser(response)


@pytest.mark.parametrize(
    ("parser", "response", "match"),
    [
        (
            parse_add_order_response,
            {"error": [], "result": {"txid": ["OUF4EM-FRGI2-MQMWZD", ""]}},
            "invalid txid values",
        ),
        (
            parse_cancel_order_response,
            {"error": [], "result": {"count": True}},
            "invalid 'count' value",
        ),
        (
            parse_cancel_order_response,
            {"error": [], "result": {"count": 1.5}},
            "integer 'count'",
        ),
        (
            parse_cancel_order_response,
            {"error": [], "result": {"count": -1}},
            "negative 'count'",
        ),
    ],
)
def test_mutation_parsers_reject_additional_malformed_results(
    parser: MutationParser,
    response: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(KrakenResponseError, match=match):
        parser(response)


def test_safe_mode_blocked_error_can_be_caught_distinctly() -> None:
    caught: ExchangeError | None = None

    try:
        raise SafeModeBlockedError("Order mutations are disabled.")
    except SafeModeBlockedError as exc:
        caught = exc
    except ExchangeError as exc:  # pragma: no cover
        pytest.fail(f"Expected SafeModeBlockedError, got {type(exc).__name__}")

    assert isinstance(caught, SafeModeBlockedError)
    assert str(caught) == "Order mutations are disabled."
