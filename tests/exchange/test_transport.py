from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
from http.client import HTTPResponse
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from core.errors import (
    InsufficientFundsError,
    KrakenAPIError,
    RateLimitExceededError,
)
from exchange.transport import (
    KRAKEN_BASE_URL,
    HttpKrakenTransport,
    KrakenAuthError,
    RetryPolicy,
    SignedKrakenRequest,
    make_default_nonce_source,
    sign_request,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_API_KEY = "test_key"
TEST_API_SECRET = base64.b64encode(b"test_secret").decode()
TEST_ENDPOINT = "/0/private/Balance"


def _make_http_response(body: dict) -> HTTPResponse:
    """Build a minimal HTTPResponse-like object from a dict."""
    raw = json.dumps(body).encode()
    mock_resp = MagicMock(spec=HTTPResponse)
    mock_resp.read.return_value = raw
    return mock_resp


# ---------------------------------------------------------------------------
# sign_request tests
# ---------------------------------------------------------------------------


def test_sign_request_produces_valid_signature() -> None:
    """Verify the HMAC-SHA512 signature matches a manual computation."""
    fixed_nonce = 1_000_000
    nonce_source = lambda: fixed_nonce  # noqa: E731

    result = sign_request(
        TEST_API_KEY,
        TEST_API_SECRET,
        TEST_ENDPOINT,
        {},
        nonce_source=nonce_source,
    )

    # Manual computation
    post_data = urllib.parse.urlencode({"nonce": fixed_nonce})
    sha256_digest = hashlib.sha256((str(fixed_nonce) + post_data).encode()).digest()
    message = TEST_ENDPOINT.encode() + sha256_digest
    secret_bytes = base64.b64decode(TEST_API_SECRET)
    expected_mac = hmac.new(secret_bytes, message, hashlib.sha512).digest()
    expected_signature = base64.b64encode(expected_mac).decode()

    assert result.headers["API-Sign"] == expected_signature
    assert result.headers["API-Key"] == TEST_API_KEY
    assert result.url == KRAKEN_BASE_URL + TEST_ENDPOINT


def test_sign_request_nonce_included_in_body() -> None:
    """The nonce must appear in the POST body."""
    fixed_nonce = 42
    result = sign_request(
        TEST_API_KEY,
        TEST_API_SECRET,
        TEST_ENDPOINT,
        {},
        nonce_source=lambda: fixed_nonce,
    )
    parsed = urllib.parse.parse_qs(result.body.decode())
    assert parsed["nonce"] == [str(fixed_nonce)]


def test_sign_request_payload_merged_with_nonce() -> None:
    """Extra payload fields must appear alongside the nonce."""
    fixed_nonce = 99
    result = sign_request(
        TEST_API_KEY,
        TEST_API_SECRET,
        TEST_ENDPOINT,
        {"pair": "XXBTZUSD", "type": "buy"},
        nonce_source=lambda: fixed_nonce,
    )
    parsed = urllib.parse.parse_qs(result.body.decode())
    assert parsed["nonce"] == [str(fixed_nonce)]
    assert parsed["pair"] == ["XXBTZUSD"]
    assert parsed["type"] == ["buy"]


def test_default_nonce_source_strictly_increasing() -> None:
    """Default nonce source must return strictly increasing integers."""
    source = make_default_nonce_source()
    values = [source() for _ in range(100)]
    for i in range(1, len(values)):
        assert values[i] > values[i - 1], f"values[{i}] <= values[{i - 1}]"


def test_auth_error_on_bad_secret() -> None:
    """Non-base64 API secret must raise KrakenAuthError."""
    with pytest.raises(KrakenAuthError):
        sign_request(
            TEST_API_KEY,
            "%%%not-valid-base64%%%",
            TEST_ENDPOINT,
            {},
            nonce_source=lambda: 1,
        )


# ---------------------------------------------------------------------------
# HttpKrakenTransport tests
# ---------------------------------------------------------------------------


def _signed_request() -> SignedKrakenRequest:
    """Convenience: a pre-built signed request for transport tests."""
    return sign_request(
        TEST_API_KEY,
        TEST_API_SECRET,
        TEST_ENDPOINT,
        {},
        nonce_source=lambda: 1,
    )


def test_transport_send_success() -> None:
    """Successful Kraken response returns the result dict."""
    body = {"error": [], "result": {"ZUSD": "100"}}
    sender = MagicMock(return_value=_make_http_response(body))
    transport = HttpKrakenTransport(sender=sender)

    result = transport.send(_signed_request())

    assert result == {"ZUSD": "100"}
    sender.assert_called_once()


def test_transport_send_kraken_error_raises() -> None:
    """Generic Kraken API error is raised immediately."""
    body = {"error": ["EGeneral:Invalid arguments"], "result": None}
    sender = MagicMock(return_value=_make_http_response(body))
    transport = HttpKrakenTransport(sender=sender)

    with pytest.raises(KrakenAPIError, match="EGeneral:Invalid arguments"):
        transport.send(_signed_request())


def test_transport_send_rate_limit_error() -> None:
    """Rate limit error is classified as RateLimitExceededError."""
    body = {"error": ["EAPI:Rate limit exceeded"]}
    sender = MagicMock(return_value=_make_http_response(body))
    transport = HttpKrakenTransport(
        sender=sender,
        retry_policy=RetryPolicy(max_retries=0),
    )

    with pytest.raises(RateLimitExceededError):
        transport.send(_signed_request())


def test_transport_send_insufficient_funds_error() -> None:
    """Insufficient funds error is classified as InsufficientFundsError."""
    body = {"error": ["EOrder:Insufficient funds"]}
    sender = MagicMock(return_value=_make_http_response(body))
    transport = HttpKrakenTransport(sender=sender)

    with pytest.raises(InsufficientFundsError):
        transport.send(_signed_request())


def test_transport_send_retries_on_transient_error() -> None:
    """Transport retries on URLError then succeeds."""
    success_body = {"error": [], "result": {"balance": "42"}}
    call_count = 0

    def flaky_sender(req, timeout):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise URLError("connection reset")
        return _make_http_response(success_body)

    transport = HttpKrakenTransport(
        sender=flaky_sender,
        retry_policy=RetryPolicy(max_retries=3, base_delay_sec=0.0, max_delay_sec=0.0),
    )

    result = transport.send(_signed_request())

    assert result == {"balance": "42"}
    assert call_count == 3


def test_transport_send_no_retry_on_non_retryable() -> None:
    """Non-retryable KrakenAPIError is raised immediately with no retry."""
    body = {"error": ["EGeneral:Invalid arguments"], "result": None}
    sender = MagicMock(return_value=_make_http_response(body))
    transport = HttpKrakenTransport(
        sender=sender,
        retry_policy=RetryPolicy(max_retries=3, base_delay_sec=0.0, max_delay_sec=0.0),
    )

    with pytest.raises(KrakenAPIError):
        transport.send(_signed_request())

    assert sender.call_count == 1


def test_transport_send_exhausts_retries() -> None:
    """After max_retries+1 attempts, the last transient error is raised."""
    call_count = 0

    def always_fail(req, timeout):
        nonlocal call_count
        call_count += 1
        raise URLError("down")

    transport = HttpKrakenTransport(
        sender=always_fail,
        retry_policy=RetryPolicy(max_retries=2, base_delay_sec=0.0, max_delay_sec=0.0),
    )

    with pytest.raises(URLError):
        transport.send(_signed_request())

    assert call_count == 3  # initial + 2 retries
