from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from types import MappingProxyType
from typing import Protocol

from core.config import (
    DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SEC,
    DEFAULT_CIRCUIT_BREAKER_THRESHOLD,
    DEFAULT_CIRCUIT_BREAKER_WINDOW_SEC,
)
from core.errors import ExchangeError, RateLimitExceededError
from core.types import CircuitBreakerState, OrderRequest, OrderType
from exchange.client import PreparedKrakenRequest
from exchange.pair_metadata import PairMetadataCache
from exchange.symbols import normalize_pair

TimeSource = Callable[[], float]
SequenceSource = Callable[[], int]


class OrderGateError(ExchangeError):
    """Base exception for order-gate failures."""


class InvalidOrderRequestError(OrderGateError):
    """Raised when an order request cannot be translated into a Kraken payload."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class OrderMutationBlockedError(OrderGateError):
    """Raised when the circuit breaker blocks a mutation request."""

    def __init__(self, *, resume_at: float) -> None:
        self.resume_at = resume_at
        super().__init__(f"Order mutations are blocked until monotonic time {resume_at}.")


class PairNotAllowedError(OrderGateError):
    """Raised when an order targets a pair not in the allowed set."""

    def __init__(self, pair: str, allowed: frozenset[str]) -> None:
        self.pair = pair
        self.allowed = allowed
        super().__init__(f"Pair {pair!r} is not in the allowed set: {sorted(allowed)}.")


class OrderBelowMinimumError(OrderGateError):
    """Raised when order quantity is below the exchange minimum."""

    def __init__(self, pair: str, quantity: Decimal, minimum: Decimal) -> None:
        self.pair = pair
        self.quantity = quantity
        self.minimum = minimum
        super().__init__(
            f"Order quantity {quantity} for {pair!r} is below minimum {minimum}."
        )


@dataclass(frozen=True, slots=True)
class CircuitBreakerPolicy:
    threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD
    window_seconds: int = DEFAULT_CIRCUIT_BREAKER_WINDOW_SEC
    cooldown_seconds: int = DEFAULT_CIRCUIT_BREAKER_COOLDOWN_SEC


@dataclass(frozen=True, slots=True)
class CircuitBreakerSnapshot:
    state: CircuitBreakerState
    failure_count: int
    opened_until: float | None = None


class OrderGatewayClient(Protocol):
    def place_order(
        self,
        pair: str,
        payload: Mapping[str, object],
    ) -> PreparedKrakenRequest:
        ...

    def cancel_order(
        self,
        pair: str,
        txid: str,
        *,
        order_age_seconds: float,
    ) -> PreparedKrakenRequest:
        ...


class OrderMutationCircuitBreaker:
    """Small mutation breaker that opens after repeated exchange failures."""

    def __init__(
        self,
        *,
        policy: CircuitBreakerPolicy,
        now: TimeSource | None = None,
    ) -> None:
        self._policy = policy
        self._now = time.monotonic if now is None else now
        self._failures: deque[float] = deque()
        self._opened_at: float | None = None

    def before_mutation(self) -> None:
        now = self._now()
        self._prune_failures(now)
        if self._opened_at is None:
            return

        opened_until = self._opened_at + self._policy.cooldown_seconds
        if now < opened_until:
            raise OrderMutationBlockedError(resume_at=opened_until)

        self._opened_at = None
        self._failures.clear()

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None

    def record_failure(self) -> None:
        now = self._now()
        self._prune_failures(now)
        self._failures.append(now)
        if len(self._failures) >= self._policy.threshold:
            self._opened_at = now

    def snapshot(self) -> CircuitBreakerSnapshot:
        now = self._now()
        self._prune_failures(now)
        if self._opened_at is None:
            return CircuitBreakerSnapshot(
                state=CircuitBreakerState.CLOSED,
                failure_count=len(self._failures),
            )

        opened_until = self._opened_at + self._policy.cooldown_seconds
        if now < opened_until:
            state = CircuitBreakerState.OPEN
        else:
            state = CircuitBreakerState.HALF_OPEN
        return CircuitBreakerSnapshot(
            state=state,
            failure_count=len(self._failures),
            opened_until=opened_until,
        )

    def _prune_failures(self, now: float) -> None:
        cutoff = now - self._policy.window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()


class OrderGate:
    """Single point for building outbound order payloads and mutating the exchange."""

    def __init__(
        self,
        *,
        client: OrderGatewayClient,
        order_prefix: str = "kbv4",
        breaker_policy: CircuitBreakerPolicy = CircuitBreakerPolicy(),
        now: TimeSource | None = None,
        sequence_source: SequenceSource | None = None,
        allowed_pairs: frozenset[str] = frozenset(),
        kraken_tier: str = "starter",
        pair_metadata: PairMetadataCache | None = None,
    ) -> None:
        self._client = client
        self._order_prefix = order_prefix
        self._pair_metadata = pair_metadata
        self._sequence_source = count(1).__next__ if sequence_source is None else sequence_source
        self._breaker = OrderMutationCircuitBreaker(policy=breaker_policy, now=now)
        self._allowed_pairs = allowed_pairs
        self._kraken_tier = kraken_tier

    @property
    def circuit_breaker(self) -> CircuitBreakerSnapshot:
        return self._breaker.snapshot()

    def _ensure_pair_allowed(self, pair: str) -> None:
        if not self._allowed_pairs:
            return
        normalized = normalize_pair(pair)
        if normalized not in self._allowed_pairs:
            raise PairNotAllowedError(normalized, self._allowed_pairs)

    def place_order(self, order: OrderRequest) -> PreparedKrakenRequest:
        self._ensure_pair_allowed(order.pair)
        self._breaker.before_mutation()
        payload = self.build_order_payload(order)
        try:
            request = self._client.place_order(order.pair, payload)
        except RateLimitExceededError:
            raise  # Rate limits don't count toward circuit breaker
        except ExchangeError:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return request

    def cancel_order(
        self,
        pair: str,
        order_id: str,
        *,
        order_age_seconds: float,
    ) -> PreparedKrakenRequest:
        self._ensure_pair_allowed(pair)
        if not order_id.strip():
            raise InvalidOrderRequestError("Cancel requests require a non-empty order_id.")

        self._breaker.before_mutation()
        try:
            request = self._client.cancel_order(
                pair,
                order_id,
                order_age_seconds=order_age_seconds,
            )
        except RateLimitExceededError:
            raise  # Rate limits don't count toward circuit breaker
        except ExchangeError:
            self._breaker.record_failure()
            raise
        self._breaker.record_success()
        return request

    def build_order_payload(self, order: OrderRequest) -> Mapping[str, object]:
        client_order_id = order.client_order_id or self._next_client_order_id(order.pair)
        payload: dict[str, object] = {
            "ordertype": _render_order_type(order.order_type),
            "type": order.side.value,
            "volume": _render_decimal(order.quantity),
        }
        # cl_ord_id is not supported on Kraken Starter tier
        if self._kraken_tier != "starter":
            payload["cl_ord_id"] = client_order_id

        if order.order_type is OrderType.MARKET:
            pass  # Market orders have no price field
        elif order.order_type is OrderType.LIMIT:
            if order.limit_price is None:
                raise InvalidOrderRequestError("Limit orders require limit_price.")
            payload["price"] = _render_decimal(order.limit_price)
        elif order.order_type is OrderType.STOP_LOSS:
            if order.stop_price is None:
                raise InvalidOrderRequestError("Stop-loss orders require stop_price.")
            payload["price"] = _render_decimal(order.stop_price)
        else:
            raise InvalidOrderRequestError(f"Unsupported order type {order.order_type!r}.")

        # Defensive ordermin check
        if self._pair_metadata is not None:
            ordermin = self._pair_metadata.ordermin(normalize_pair(order.pair))
            if ordermin is not None and order.quantity < ordermin:
                raise OrderBelowMinimumError(order.pair, order.quantity, ordermin)

        return MappingProxyType(payload)

    def _next_client_order_id(self, pair: str) -> str:
        pair_token = normalize_pair(pair).replace("/", "").lower()
        return f"{self._order_prefix}-{pair_token}-{self._sequence_source():06d}"


def _render_order_type(order_type: OrderType) -> str:
    if order_type is OrderType.MARKET:
        return "market"
    if order_type is OrderType.LIMIT:
        return "limit"
    if order_type is OrderType.STOP_LOSS:
        return "stop-loss"
    raise InvalidOrderRequestError(f"Unsupported order type {order_type!r}.")


def _render_decimal(value: Decimal) -> str:
    return format(value, "f")


__all__ = [
    "CircuitBreakerPolicy",
    "CircuitBreakerSnapshot",
    "InvalidOrderRequestError",
    "OrderGate",
    "OrderGateError",
    "OrderGatewayClient",
    "OrderMutationBlockedError",
    "OrderMutationCircuitBreaker",
    "OrderBelowMinimumError",
    "PairNotAllowedError",
]
