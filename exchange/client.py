from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from core.errors import ExchangeError, RateLimitExceededError
from exchange.symbols import normalize_asset_symbol, normalize_pair

STARTER_REST_BUCKET_MAX: Final[Decimal] = Decimal("15")
STARTER_REST_DECAY_PER_SECOND: Final[Decimal] = Decimal("0.33")
STARTER_MATCHING_ENGINE_MAX_PER_PAIR: Final[int] = 60
STARTER_CANCEL_PENALTY_THRESHOLD_SECONDS: Final[Decimal] = Decimal("5")
STARTER_CANCEL_PENALTY_POINTS: Final[int] = 8
STARTER_MATCHING_ENGINE_COST: Final[int] = 1
STARTER_MATCHING_ENGINE_DECAY_PER_SECOND: Final[Decimal] = Decimal("1")

TimeSource = Callable[[], float]
PairNormalizer = Callable[[str], str]
AssetNormalizer = Callable[[str], str]


class UnsupportedKrakenTierError(ExchangeError):
    """Raised when a client is asked to use an unsupported Kraken tier."""

    def __init__(self, tier: str) -> None:
        self.tier = tier
        super().__init__(f"Unsupported Kraken tier {tier!r}.")


class InvalidOrderAgeError(ExchangeError):
    """Raised when cancel accounting receives a negative order age."""

    def __init__(self, order_age_seconds: float) -> None:
        self.order_age_seconds = order_age_seconds
        super().__init__(f"Order age must be non-negative; got {order_age_seconds!r}.")


@dataclass(frozen=True, slots=True)
class KrakenRateLimitPolicy:
    rest_bucket_max: Decimal = STARTER_REST_BUCKET_MAX
    rest_decay_per_second: Decimal = STARTER_REST_DECAY_PER_SECOND
    matching_engine_max_per_pair: int = STARTER_MATCHING_ENGINE_MAX_PER_PAIR
    cancel_penalty_threshold_seconds: Decimal = STARTER_CANCEL_PENALTY_THRESHOLD_SECONDS
    cancel_penalty_points: int = STARTER_CANCEL_PENALTY_POINTS
    matching_engine_cost: int = STARTER_MATCHING_ENGINE_COST
    matching_engine_decay_per_second: Decimal = STARTER_MATCHING_ENGINE_DECAY_PER_SECOND


@dataclass(frozen=True, slots=True)
class RestRateLimitSnapshot:
    used_points: Decimal
    remaining_points: Decimal


@dataclass(frozen=True, slots=True)
class MatchingEngineLimitSnapshot:
    pair: str
    used_points: int
    remaining_points: int


@dataclass(frozen=True, slots=True)
class PreparedKrakenRequest:
    endpoint: str
    payload: Mapping[str, object]


STARTER_TIER_POLICY: Final[KrakenRateLimitPolicy] = KrakenRateLimitPolicy()


class KrakenRateLimiter:
    """Starter-tier rate-limit accounting for REST and per-pair matching engine budgets."""

    def __init__(
        self,
        *,
        policy: KrakenRateLimitPolicy = STARTER_TIER_POLICY,
        now: TimeSource | None = None,
    ) -> None:
        self._policy = policy
        self._now = time.monotonic if now is None else now
        self._rest_used = Decimal("0")
        self._rest_updated_at = _decimal_seconds(self._now())
        self._matching_engine_usage: dict[str, int] = {}
        self._matching_engine_updated_at: dict[str, Decimal] = {}

    def consume_rest(self, *, cost: int = 1) -> RestRateLimitSnapshot:
        now = _decimal_seconds(self._now())
        self._apply_rest_decay(now)
        cost_points = Decimal(cost)
        projected = self._rest_used + cost_points
        if projected > self._policy.rest_bucket_max:
            raise RateLimitExceededError("Starter REST rate limit exceeded.")
        self._rest_used = projected
        return self.rest_snapshot()

    def rest_snapshot(self) -> RestRateLimitSnapshot:
        now = _decimal_seconds(self._now())
        self._apply_rest_decay(now)
        remaining = self._policy.rest_bucket_max - self._rest_used
        return RestRateLimitSnapshot(
            used_points=self._rest_used,
            remaining_points=max(Decimal("0"), remaining),
        )

    def consume_matching_engine(
        self,
        pair: str,
        *,
        cost: int = STARTER_MATCHING_ENGINE_COST,
    ) -> MatchingEngineLimitSnapshot:
        normalized_pair = normalize_pair(pair)
        now = _decimal_seconds(self._now())
        self._apply_matching_engine_decay(normalized_pair, now)
        current_points = self._matching_engine_usage.get(normalized_pair, 0)
        projected = current_points + cost
        if projected > self._policy.matching_engine_max_per_pair:
            raise RateLimitExceededError(
                f"Starter matching engine rate limit exceeded for {normalized_pair}."
            )
        self._matching_engine_usage[normalized_pair] = projected
        self._matching_engine_updated_at[normalized_pair] = now
        return self.matching_engine_snapshot(normalized_pair)

    def consume_cancel(
        self,
        pair: str,
        *,
        order_age_seconds: float,
    ) -> MatchingEngineLimitSnapshot:
        age = Decimal(str(order_age_seconds))
        if age < 0:
            raise InvalidOrderAgeError(order_age_seconds)

        cost = self._policy.matching_engine_cost
        if age < self._policy.cancel_penalty_threshold_seconds:
            cost += self._policy.cancel_penalty_points
        return self.consume_matching_engine(pair, cost=cost)

    def matching_engine_snapshot(self, pair: str) -> MatchingEngineLimitSnapshot:
        normalized_pair = normalize_pair(pair)
        now = _decimal_seconds(self._now())
        self._apply_matching_engine_decay(normalized_pair, now)
        used_points = self._matching_engine_usage.get(normalized_pair, 0)
        return MatchingEngineLimitSnapshot(
            pair=normalized_pair,
            used_points=used_points,
            remaining_points=self._policy.matching_engine_max_per_pair - used_points,
        )

    def _apply_rest_decay(self, now: Decimal) -> None:
        elapsed = now - self._rest_updated_at
        if elapsed <= 0:
            return
        decay = elapsed * self._policy.rest_decay_per_second
        self._rest_used = max(Decimal("0"), self._rest_used - decay)
        self._rest_updated_at = now

    def _apply_matching_engine_decay(self, pair: str, now: Decimal) -> None:
        last = self._matching_engine_updated_at.get(pair)
        if last is None:
            self._matching_engine_updated_at[pair] = now
            return
        elapsed = now - last
        if elapsed <= 0:
            return
        decay = int(elapsed * self._policy.matching_engine_decay_per_second)
        if decay > 0:
            current = self._matching_engine_usage.get(pair, 0)
            self._matching_engine_usage[pair] = max(0, current - decay)
            self._matching_engine_updated_at[pair] = now


class KrakenClient:
    """Small Kraken REST client scaffold with shared normalization hooks."""

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        tier: str = "starter",
        rate_limiter: KrakenRateLimiter | None = None,
        pair_normalizer: PairNormalizer = normalize_pair,
        asset_normalizer: AssetNormalizer = normalize_asset_symbol,
    ) -> None:
        if tier != "starter":
            raise UnsupportedKrakenTierError(tier)

        self.api_key = api_key
        self.api_secret = api_secret
        self.tier = tier
        self._rate_limiter = KrakenRateLimiter() if rate_limiter is None else rate_limiter
        self._pair_normalizer = pair_normalizer
        self._asset_normalizer = asset_normalizer

    @property
    def rate_limiter(self) -> KrakenRateLimiter:
        return self._rate_limiter

    def normalize_pair(self, pair: str) -> str:
        return self._pair_normalizer(pair)

    def normalize_asset(self, asset: str) -> str:
        return self._asset_normalizer(asset)

    def get_balances(self) -> PreparedKrakenRequest:
        self._rate_limiter.consume_rest()
        return self._prepare_request("/0/private/Balance", {})

    def get_open_orders(self) -> PreparedKrakenRequest:
        self._rate_limiter.consume_rest()
        return self._prepare_request("/0/private/OpenOrders", {})

    def get_trade_history(self) -> PreparedKrakenRequest:
        self._rate_limiter.consume_rest(cost=2)
        return self._prepare_request("/0/private/TradesHistory", {})

    def get_asset_pairs(self) -> PreparedKrakenRequest:
        self._rate_limiter.consume_rest()
        return self._prepare_request("/0/public/AssetPairs", {})

    def place_order(
        self,
        pair: str,
        payload: Mapping[str, object],
    ) -> PreparedKrakenRequest:
        normalized_pair = self.normalize_pair(pair)
        self._rate_limiter.consume_rest()
        self._rate_limiter.consume_matching_engine(normalized_pair)

        request_payload = dict(payload)
        request_payload["pair"] = normalized_pair
        return self._prepare_request("/0/private/AddOrder", request_payload)

    def cancel_order(
        self,
        pair: str,
        txid: str,
        *,
        order_age_seconds: float,
    ) -> PreparedKrakenRequest:
        normalized_pair = self.normalize_pair(pair)
        self._rate_limiter.consume_rest()
        self._rate_limiter.consume_cancel(
            normalized_pair,
            order_age_seconds=order_age_seconds,
        )
        return self._prepare_request(
            "/0/private/CancelOrder",
            {"pair": normalized_pair, "txid": txid},
        )

    def _prepare_request(
        self,
        endpoint: str,
        payload: Mapping[str, object],
    ) -> PreparedKrakenRequest:
        return PreparedKrakenRequest(
            endpoint=endpoint,
            payload=MappingProxyType(dict(payload)),
        )


def _decimal_seconds(value: float) -> Decimal:
    return Decimal(str(value))


__all__ = [
    "InvalidOrderAgeError",
    "KrakenClient",
    "KrakenRateLimitPolicy",
    "KrakenRateLimiter",
    "MatchingEngineLimitSnapshot",
    "PreparedKrakenRequest",
    "RestRateLimitSnapshot",
    "STARTER_TIER_POLICY",
    "UnsupportedKrakenTierError",
]
