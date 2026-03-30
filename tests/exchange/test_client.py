from __future__ import annotations

from decimal import Decimal

import pytest

from core.errors import RateLimitExceededError
from exchange.client import KrakenClient, KrakenRateLimiter


class ManualClock:
    def __init__(self, current: float = 0.0) -> None:
        self.current = current

    def now(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


def test_place_order_uses_normalized_pair_and_consumes_limits() -> None:
    clock = ManualClock()
    client = KrakenClient(
        api_key="key",
        api_secret="secret",
        rate_limiter=KrakenRateLimiter(now=clock.now),
    )

    request = client.place_order("xxrpzusd", {"side": "buy", "volume": "100"})

    assert request.endpoint == "/0/private/AddOrder"
    assert request.payload["pair"] == "XRP/USD"
    assert client.rate_limiter.rest_snapshot().used_points == Decimal("1")
    assert client.rate_limiter.matching_engine_snapshot("xxrpzusd").used_points == 1


def test_rest_rate_limit_decays_over_time() -> None:
    clock = ManualClock()
    limiter = KrakenRateLimiter(now=clock.now)

    for _ in range(15):
        limiter.consume_rest()

    with pytest.raises(RateLimitExceededError):
        limiter.consume_rest()

    clock.advance(4.0)
    snapshot = limiter.consume_rest()

    assert snapshot.used_points == Decimal("14.68")
    assert snapshot.remaining_points == Decimal("0.32")


def test_early_cancel_penalty_counts_against_pair_limit() -> None:
    limiter = KrakenRateLimiter()
    limiter.consume_matching_engine("dogeusd", cost=52)

    with pytest.raises(RateLimitExceededError):
        limiter.consume_cancel("dogeusd", order_age_seconds=4.99)


def test_cancel_after_five_seconds_avoids_extra_penalty() -> None:
    limiter = KrakenRateLimiter()
    limiter.consume_matching_engine("dogeusd", cost=59)

    snapshot = limiter.consume_cancel("DOGE/USD", order_age_seconds=5.0)

    assert snapshot.pair == "DOGE/USD"
    assert snapshot.used_points == 60
    assert snapshot.remaining_points == 0


def test_get_trade_history_consumes_two_rest_points() -> None:
    clock = ManualClock()
    client = KrakenClient(
        api_key="key",
        api_secret="secret",
        rate_limiter=KrakenRateLimiter(now=clock.now),
    )

    request = client.get_trade_history()

    assert request.endpoint == "/0/private/TradesHistory"
    assert client.rate_limiter.rest_snapshot().used_points == Decimal("2")


def test_get_asset_pairs_uses_public_assetpairs_endpoint() -> None:
    clock = ManualClock()
    client = KrakenClient(
        api_key="key",
        api_secret="secret",
        rate_limiter=KrakenRateLimiter(now=clock.now),
    )

    request = client.get_asset_pairs()

    assert request.endpoint == "/0/public/AssetPairs"
    assert dict(request.payload) == {}
    assert client.rate_limiter.rest_snapshot().used_points == Decimal("1")
