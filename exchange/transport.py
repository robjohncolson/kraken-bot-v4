from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPResponse
from typing import Final
from urllib.error import HTTPError, URLError

from core.errors import (
    ExchangeError,
    InsufficientFundsError,
    KrakenAPIError,
    OrderRejectedError,
    RateLimitExceededError,
)

logger = logging.getLogger(__name__)

KRAKEN_BASE_URL: Final[str] = "https://api.kraken.com"

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

NonceSource = Callable[[], int]
"""Injectable nonce factory.  Must return a strictly increasing integer."""

Sender = Callable[[urllib.request.Request, float], HTTPResponse]
"""Injectable HTTP sender.  Receives (request, timeout_sec) and returns a response."""

# ---------------------------------------------------------------------------
# Module-local errors
# ---------------------------------------------------------------------------


class KrakenTransportError(ExchangeError):
    """Non-retryable transport failure (e.g. network / TLS)."""


class KrakenAuthError(ExchangeError):
    """Signing failure such as a malformed base64 API secret."""


# ---------------------------------------------------------------------------
# Default nonce source
# ---------------------------------------------------------------------------


def make_default_nonce_source() -> NonceSource:
    """Return a nonce source seeded from wall-clock microseconds, then monotonically incrementing."""
    state = {"value": int(time.time() * 1_000_000)}

    def _next_nonce() -> int:
        state["value"] += 1
        return state["value"]

    return _next_nonce


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignedKrakenRequest:
    """Immutable container for a fully-signed Kraken REST request."""

    url: str
    headers: Mapping[str, str]
    body: bytes


def sign_request(
    api_key: str,
    api_secret: str,
    endpoint: str,
    payload: Mapping[str, object],
    *,
    nonce_source: NonceSource,
) -> SignedKrakenRequest:
    """Build an HMAC-SHA512-signed request for a Kraken private endpoint.

    Parameters
    ----------
    api_key:
        Kraken API key.
    api_secret:
        Kraken API secret (base64-encoded).
    endpoint:
        REST path, e.g. ``/0/private/Balance``.
    payload:
        Additional POST fields.  Any ``"nonce"`` key in *payload* is
        overwritten — the transport owns the nonce.
    nonce_source:
        Callable returning a strictly-increasing integer.  Must be
        shared across all calls for the lifetime of the process.
    """
    nonce = nonce_source()

    # Transport owns the nonce — strip any caller-supplied value first.
    clean_payload = {k: v for k, v in payload.items() if k != "nonce"}
    post_data = urllib.parse.urlencode({"nonce": nonce, **clean_payload})

    sha256_digest = hashlib.sha256((str(nonce) + post_data).encode()).digest()
    message = endpoint.encode() + sha256_digest

    try:
        secret_bytes = base64.b64decode(api_secret)
    except Exception as exc:
        raise KrakenAuthError(f"Failed to decode API secret: {exc}") from exc

    mac = hmac.new(secret_bytes, message, hashlib.sha512).digest()
    signature = base64.b64encode(mac).decode()

    return SignedKrakenRequest(
        url=KRAKEN_BASE_URL + endpoint,
        headers={
            "API-Key": api_key,
            "API-Sign": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=post_data.encode(),
    )


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def _classify_kraken_errors(errors: list[str]) -> KrakenAPIError:
    """Map the first actionable Kraken error string to a typed exception."""
    for err in errors:
        if err.startswith("EAPI:Rate limit"):
            return RateLimitExceededError(err)
        if err == "EOrder:Insufficient funds":
            return InsufficientFundsError(err)
        if err.startswith("EOrder:"):
            return OrderRejectedError(err)
    # Fall back to generic API error with all messages joined.
    return KrakenAPIError("; ".join(errors))


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential-backoff configuration for *read-only* retries."""

    max_retries: int = 3
    base_delay_sec: float = 2.0
    max_delay_sec: float = 15.0


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


def _default_sender(request: urllib.request.Request, timeout_sec: float) -> HTTPResponse:
    return urllib.request.urlopen(request, timeout=timeout_sec)  # noqa: S310


class HttpKrakenTransport:
    """Send signed requests to Kraken with read-only retry on transient errors."""

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy = RetryPolicy(),
        timeout_sec: float = 15.0,
        sender: Sender | None = None,
    ) -> None:
        self._retry_policy = retry_policy
        self._timeout_sec = timeout_sec
        self._sender: Sender = _default_sender if sender is None else sender

    def send(self, request: SignedKrakenRequest) -> dict[str, object]:
        """Execute *request* and return the ``result`` payload.

        Retries on transient errors (5xx, ``URLError``, ``RateLimitExceededError``)
        up to ``retry_policy.max_retries`` times with exponential backoff.
        Non-retryable errors are raised immediately.
        """
        last_exc: Exception | None = None

        for attempt in range(self._retry_policy.max_retries + 1):
            try:
                return self._attempt(request)
            except (RateLimitExceededError, URLError) as exc:
                last_exc = exc
                self._maybe_retry(attempt, exc)
            except HTTPError as exc:
                if 500 <= exc.code < 600:
                    last_exc = exc
                    self._maybe_retry(attempt, exc)
                else:
                    raise KrakenTransportError(
                        f"HTTP {exc.code}: {exc.reason}"
                    ) from exc

        # All retries exhausted — re-raise the last transient error.
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _attempt(self, request: SignedKrakenRequest) -> dict[str, object]:
        """Single HTTP round-trip + JSON parse + Kraken error check."""
        req = urllib.request.Request(
            request.url,
            data=request.body,
            headers=dict(request.headers),
            method="POST",
        )
        resp = self._sender(req, self._timeout_sec)
        raw = resp.read()
        data: dict[str, object] = json.loads(raw)

        errors = data.get("error")
        if errors:
            assert isinstance(errors, list)
            raise _classify_kraken_errors(errors)

        return data["result"]  # type: ignore[return-value]

    def _maybe_retry(self, attempt: int, exc: Exception) -> None:
        """Sleep with exponential backoff or raise if retries exhausted."""
        if attempt >= self._retry_policy.max_retries:
            raise exc
        delay = min(
            self._retry_policy.base_delay_sec * (2 ** attempt),
            self._retry_policy.max_delay_sec,
        )
        logger.warning(
            "Transient error on attempt %d/%d, retrying in %.1fs: %s",
            attempt + 1,
            self._retry_policy.max_retries + 1,
            delay,
            exc,
        )
        time.sleep(delay)


__all__ = [
    "KRAKEN_BASE_URL",
    "HttpKrakenTransport",
    "KrakenAuthError",
    "KrakenTransportError",
    "NonceSource",
    "RetryPolicy",
    "Sender",
    "SignedKrakenRequest",
    "make_default_nonce_source",
    "sign_request",
]
