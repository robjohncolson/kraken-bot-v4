from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

import pandas as pd

from beliefs.consensus import compute_consensus
from core.config import Settings
from core.types import BeliefDirection, BullCandidate, DurationEstimate, ZERO_DECIMAL
from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv
from scheduler import SchedulerState
from trading.duration_estimator import DurationEstimatorError, estimate_bear_duration
from trading.pair_scanner import PairScanner

logger = logging.getLogger(__name__)

DOGE_PAIR: Final[str] = "DOGE/USD"
SCAN_INTERVAL_MINUTES: Final[int] = 60
SCAN_BAR_COUNT: Final[int] = 50

OHLCVFetcher = Callable[..., pd.DataFrame]


@dataclass(frozen=True, slots=True)
class ConditionalTreeState:
    is_active: bool = False
    trigger_time: datetime | None = None
    bear_estimate: DurationEstimate | None = None
    chosen_candidate: BullCandidate | None = None
    exit_deadline: datetime | None = None


class ConditionalTreeCoordinator:
    """Plan a temporary USD rotation after bearish DOGE consensus."""

    def __init__(
        self,
        *,
        settings: Settings,
        pair_scanner: PairScanner,
        ohlcv_fetcher: OHLCVFetcher = fetch_ohlcv,
    ) -> None:
        self._settings = settings
        self._pair_scanner = pair_scanner
        self._ohlcv_fetcher = ohlcv_fetcher

    def maybe_plan(
        self,
        *,
        state: SchedulerState,
        tree_state: ConditionalTreeState,
        now: datetime,
    ) -> ConditionalTreeState | None:
        if tree_state.is_active:
            return None
        if not _doge_consensus_is_bearish(state):
            return None
        if _free_usd(state) < Decimal(self._settings.min_position_usd):
            return None

        try:
            bars = self._ohlcv_fetcher(
                DOGE_PAIR,
                interval=SCAN_INTERVAL_MINUTES,
                count=SCAN_BAR_COUNT,
                timeout=self._settings.scanner_timeout_sec,
            )
            bear_estimate = estimate_bear_duration(bars)
        except (DurationEstimatorError, OHLCVFetchError) as exc:
            logger.warning("Conditional tree skipped: DOGE bear estimate failed: %s", exc)
            return None

        if bear_estimate.estimated_bear_hours <= 0:
            return None

        chosen_candidate = _select_candidate(
            self._pair_scanner.scan_bull_candidates(),
            max_peak_hours=bear_estimate.estimated_bear_hours,
        )
        if chosen_candidate is None:
            return None

        window_hours = min(
            chosen_candidate.estimated_peak_hours,
            bear_estimate.estimated_bear_hours,
        )
        return ConditionalTreeState(
            is_active=True,
            trigger_time=now,
            bear_estimate=bear_estimate,
            chosen_candidate=chosen_candidate,
            # L3.7 will move this from a planner-side placeholder into expiry handling.
            exit_deadline=now + timedelta(hours=window_hours),
        )


def _doge_consensus_is_bearish(state: SchedulerState) -> bool:
    doge_beliefs = [
        belief for belief in state.bot_state.beliefs if belief.pair == DOGE_PAIR
    ]
    consensus = compute_consensus(doge_beliefs)
    return consensus.agreed_direction is BeliefDirection.BEARISH


def _free_usd(state: SchedulerState) -> Decimal:
    reserved = sum(
        (
            pending.quote_qty
            for pending in state.bot_state.pending_orders
            if pending.kind == "position_entry" and pending.filled_qty < pending.base_qty
        ),
        start=ZERO_DECIMAL,
    )
    return max(state.bot_state.portfolio.cash_usd - reserved, ZERO_DECIMAL)


def _select_candidate(
    candidates: tuple[BullCandidate, ...],
    *,
    max_peak_hours: int,
) -> BullCandidate | None:
    for candidate in candidates:
        if candidate.pair == DOGE_PAIR:
            continue
        if candidate.estimated_peak_hours <= max_peak_hours:
            return candidate
    return None


__all__ = [
    "ConditionalTreeCoordinator",
    "ConditionalTreeState",
]
