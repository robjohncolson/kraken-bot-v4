from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from core.config import Settings, load_settings
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    BotState,
    BullCandidate,
    MarketRegime,
    Portfolio,
)
from scheduler import SchedulerState
from trading.conditional_tree import ConditionalTreeCoordinator, ConditionalTreeState

NOW = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
REQUIRED_ENV = {
    "KRAKEN_API_KEY": "kraken-key",
    "KRAKEN_API_SECRET": "kraken-secret",
}


class FakePairScanner:
    def __init__(self, candidates: tuple[BullCandidate, ...]) -> None:
        self._candidates = candidates
        self.calls = 0

    def scan_bull_candidates(self) -> tuple[BullCandidate, ...]:
        self.calls += 1
        return self._candidates


def test_maybe_plan_activates_rotation_for_bearish_doge_with_fitting_candidate() -> None:
    scanner = FakePairScanner(
        (
            _candidate("BTC/USD", confidence=0.91, peak_hours=6, price=Decimal("101")),
        )
    )
    coordinator = ConditionalTreeCoordinator(
        settings=_settings(),
        pair_scanner=scanner,
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_downtrend_closes()),
    )

    plan = coordinator.maybe_plan(
        state=_scheduler_state(cash_usd=Decimal("25")),
        tree_state=ConditionalTreeState(),
        now=NOW,
    )

    assert plan is not None
    assert plan.is_active is True
    assert plan.bear_estimate is not None
    assert plan.bear_estimate.estimated_bear_hours == 12
    assert plan.chosen_candidate is not None
    assert plan.chosen_candidate.pair == "BTC/USD"
    assert plan.trigger_time == NOW
    assert plan.expires_at == NOW + timedelta(hours=6)
    assert plan.exit_deadline == NOW + timedelta(hours=6)
    assert scanner.calls == 1


def test_maybe_plan_returns_none_without_free_usd() -> None:
    scanner = FakePairScanner(
        (
            _candidate("BTC/USD", confidence=0.91, peak_hours=6, price=Decimal("101")),
        )
    )
    coordinator = ConditionalTreeCoordinator(
        settings=_settings(),
        pair_scanner=scanner,
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_downtrend_closes()),
    )

    plan = coordinator.maybe_plan(
        state=_scheduler_state(cash_usd=Decimal("5")),
        tree_state=ConditionalTreeState(),
        now=NOW,
    )

    assert plan is None
    assert scanner.calls == 0


def test_maybe_plan_filters_out_candidates_that_outlive_bear_window() -> None:
    scanner = FakePairScanner(
        (
            _candidate("BTC/USD", confidence=0.95, peak_hours=24, price=Decimal("101")),
            _candidate("ETH/USD", confidence=0.80, peak_hours=6, price=Decimal("88")),
        )
    )
    coordinator = ConditionalTreeCoordinator(
        settings=_settings(),
        pair_scanner=scanner,
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_mixed_bearish_closes()),
    )

    plan = coordinator.maybe_plan(
        state=_scheduler_state(cash_usd=Decimal("25")),
        tree_state=ConditionalTreeState(),
        now=NOW,
    )

    assert plan is not None
    assert plan.bear_estimate is not None
    assert plan.bear_estimate.estimated_bear_hours == 12
    assert plan.chosen_candidate is not None
    assert plan.chosen_candidate.pair == "ETH/USD"
    assert plan.expires_at == NOW + timedelta(hours=6)
    assert plan.exit_deadline == NOW + timedelta(hours=6)


def test_maybe_plan_returns_none_when_no_candidate_fits_bear_window() -> None:
    scanner = FakePairScanner(
        (
            _candidate("BTC/USD", confidence=0.95, peak_hours=24, price=Decimal("101")),
        )
    )
    coordinator = ConditionalTreeCoordinator(
        settings=_settings(),
        pair_scanner=scanner,
        ohlcv_fetcher=lambda pair, **kwargs: _bars_from_close(_mixed_bearish_closes()),
    )

    plan = coordinator.maybe_plan(
        state=_scheduler_state(cash_usd=Decimal("25")),
        tree_state=ConditionalTreeState(),
        now=NOW,
    )

    assert plan is None


def _settings() -> Settings:
    return load_settings({**REQUIRED_ENV, "ENABLE_CONDITIONAL_TREE": "true"})


def _scheduler_state(*, cash_usd: Decimal) -> SchedulerState:
    state = SchedulerState(
        bot_state=BotState(
            portfolio=Portfolio(cash_usd=cash_usd),
            beliefs=(
                BeliefSnapshot(
                    pair="DOGE/USD",
                    direction=BeliefDirection.BEARISH,
                    confidence=0.88,
                    regime=MarketRegime.TRENDING,
                    sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
                ),
            ),
        ),
        now=NOW,
    )
    return replace(state, current_prices={})


def _candidate(
    pair: str,
    *,
    confidence: float,
    peak_hours: int,
    price: Decimal,
) -> BullCandidate:
    belief = BeliefSnapshot(
        pair=pair,
        direction=BeliefDirection.BULLISH,
        confidence=confidence,
        regime=MarketRegime.TRENDING,
        sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
    )
    return BullCandidate(
        pair=pair,
        belief=belief,
        confidence=confidence,
        reference_price_hint=price,
        estimated_peak_hours=peak_hours,
    )


def _bars_from_close(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "open": close * 1.01,
                "high": close * 1.02,
                "low": close * 0.98,
                "close": close,
                "volume": 1000.0,
            }
            for close in closes
        ]
    )


def _downtrend_closes() -> list[float]:
    return [120.0 - float(index) for index in range(40)]


def _mixed_bearish_closes() -> list[float]:
    closes = [100.0 for _ in range(20)]
    closes.extend([99.5, 99.0, 98.6, 98.4, 98.1, 97.9, 97.6, 97.3, 97.0, 96.8])
    closes.extend([96.7, 96.6, 96.5, 96.4, 96.3, 96.2, 96.1, 96.0, 95.9, 95.8])
    return closes
