from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import httpx
import pandas as pd

from beliefs.technical_ensemble_source import TechnicalEnsembleSource
from core.config import Settings
from core.errors import ExchangeError
from core.types import BeliefDirection, BullCandidate, OrderSide, RotationCandidate
from exchange.client import KrakenClient
from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv
from exchange.symbols import (
    SymbolNormalizationError,
    normalize_asset_symbol,
    normalize_pair,
    split_normalized_pair,
)
from exchange.transport import KRAKEN_BASE_URL

logger = logging.getLogger(__name__)

SCAN_INTERVAL_MINUTES: Final[int] = 60
SCAN_BAR_COUNT: Final[int] = 50
EMA_FAST_SPAN: Final[int] = 12
EMA_SLOW_SPAN: Final[int] = 26
MACD_SIGNAL_SPAN: Final[int] = 9
RSI_PERIOD: Final[int] = 14
RSI_BULLISH_THRESHOLD: Final[float] = 50.0
RSI_OVERBOUGHT_THRESHOLD: Final[float] = 70.0
PEAK_HOUR_BUCKETS: Final[tuple[int, ...]] = (0, 6, 12, 24)

TimeSource = Callable[[], float]
AssetPairsFetcher = Callable[[KrakenClient, float], Mapping[str, object]]
OHLCVFetcher = Callable[..., pd.DataFrame]


class PairScannerError(ExchangeError):
    """Base exception for market-scanner failures."""


class AssetPairDiscoveryError(PairScannerError):
    """Raised when Kraken AssetPairs discovery fails."""


@dataclass(frozen=True, slots=True)
class _PairDiscoveryCacheEntry:
    pairs: tuple[str, ...]
    expires_at: float


class PairScanner:
    """Rate-limit-aware scanner for bullish Kraken USD spot pairs."""

    def __init__(
        self,
        *,
        client: KrakenClient,
        settings: Settings,
        technical_source: TechnicalEnsembleSource | None = None,
        asset_pairs_fetcher: AssetPairsFetcher | None = None,
        ohlcv_fetcher: OHLCVFetcher = fetch_ohlcv,
        time_source: TimeSource = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._technical_source = technical_source or TechnicalEnsembleSource()
        self._asset_pairs_fetcher = asset_pairs_fetcher or _fetch_asset_pairs_http
        self._ohlcv_fetcher = ohlcv_fetcher
        self._time_source = time_source
        self._pair_cache: _PairDiscoveryCacheEntry | None = None

    def discover_usd_spot_pairs(self) -> tuple[str, ...]:
        now = self._time_source()
        cached = self._pair_cache
        if cached is not None and now < cached.expires_at:
            return cached.pairs

        raw_pairs = self._asset_pairs_fetcher(
            self._client,
            self._settings.scanner_timeout_sec,
        )
        pairs = _normalize_usd_spot_pairs(raw_pairs)
        self._pair_cache = _PairDiscoveryCacheEntry(
            pairs=pairs,
            expires_at=now + self._settings.scanner_pair_discovery_ttl_sec,
        )
        return pairs

    def scan_bull_candidates(self) -> tuple[BullCandidate, ...]:
        try:
            pairs = self.discover_usd_spot_pairs()
        except PairScannerError as exc:
            logger.warning("Pair scan aborted during pair discovery: %s", exc)
            return ()

        if not pairs:
            return ()

        max_workers = max(1, self._settings.scanner_max_concurrency)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures: dict[Future[BullCandidate | None], str] = {}
        timed_out = False
        try:
            for pair in pairs:
                future = executor.submit(self._scan_pair, pair)
                futures[future] = pair

            candidates: list[BullCandidate] = []
            for future in as_completed(
                futures, timeout=self._settings.scanner_timeout_sec
            ):
                candidate = future.result()
                if candidate is not None:
                    candidates.append(candidate)
        except FuturesTimeoutError:
            logger.warning(
                "Pair scan timed out after %.2fs",
                self._settings.scanner_timeout_sec,
            )
            timed_out = True
            executor.shutdown(wait=False, cancel_futures=True)
            return ()
        finally:
            if not timed_out:
                executor.shutdown(wait=True, cancel_futures=False)

        ranked = sorted(
            candidates,
            key=lambda item: (-item.confidence, item.estimated_peak_hours, item.pair),
        )
        return tuple(ranked)

    def _scan_pair(self, pair: str) -> BullCandidate | None:
        try:
            bars = self._ohlcv_fetcher(
                pair,
                interval=SCAN_INTERVAL_MINUTES,
                count=max(SCAN_BAR_COUNT, self._technical_source.min_bars),
                timeout=self._settings.scanner_timeout_sec,
            )
        except OHLCVFetchError as exc:
            logger.warning("Pair scan skipped %s due to OHLCV failure: %s", pair, exc)
            return None

        if len(bars) < self._technical_source.min_bars:
            logger.warning(
                "Pair scan skipped %s: only %d bars (need %d)",
                pair,
                len(bars),
                self._technical_source.min_bars,
            )
            return None

        close_f = bars["close"].astype(float)
        vol_f = bars["volume"].astype(float)
        recent_24 = min(24, len(bars))
        usd_volume_24h = float((vol_f.iloc[-recent_24:] * close_f.iloc[-recent_24:]).sum())
        if usd_volume_24h < self._settings.scanner_min_24h_volume_usd:
            logger.debug(
                "Skipped %s: 24h vol $%.0f < $%.0f",
                pair,
                usd_volume_24h,
                self._settings.scanner_min_24h_volume_usd,
            )
            return None

        high_f = bars["high"].astype(float)
        low_f = bars["low"].astype(float)
        recent_6 = min(6, len(bars))
        spread_pct = float(
            (
                (high_f.iloc[-recent_6:] - low_f.iloc[-recent_6:])
                / close_f.iloc[-recent_6:]
            ).mean()
            * 100
        )
        if spread_pct > self._settings.scanner_max_spread_pct:
            logger.debug(
                "Skipped %s: spread %.2f%% > %.2f%%",
                pair,
                spread_pct,
                self._settings.scanner_max_spread_pct,
            )
            return None

        try:
            belief = self._technical_source.analyze(pair, bars)
        except Exception as exc:
            logger.warning(
                "Pair scan skipped %s due to analysis failure: %s", pair, exc
            )
            return None

        if belief.direction is not BeliefDirection.BULLISH:
            return None

        try:
            estimated_peak_hours = _estimate_bull_peak_hours(bars)
        except ValueError as exc:
            logger.warning(
                "Pair scan skipped %s due to peak-hour estimation failure: %s",
                pair,
                exc,
            )
            return None
        if estimated_peak_hours <= 0:
            return None

        reference_price_hint = Decimal(
            str(pd.to_numeric(bars["close"], errors="raise").iloc[-1])
        )
        return BullCandidate(
            pair=pair,
            belief=belief,
            confidence=belief.confidence,
            reference_price_hint=reference_price_hint,
            estimated_peak_hours=estimated_peak_hours,
        )

    def discover_asset_pairs(
        self, source_asset: str
    ) -> tuple[tuple[str, str, str], ...]:
        """Discover all spot pairs involving source_asset.

        Returns tuples of (normalized_pair, base, quote) where source_asset
        is either base or quote.
        """
        now = self._time_source()
        cached = self._pair_cache
        if cached is None or now >= cached.expires_at:
            raw_pairs = self._asset_pairs_fetcher(
                self._client,
                self._settings.scanner_timeout_sec,
            )
            all_pairs = _normalize_all_spot_pairs(raw_pairs)
            self._pair_cache = _PairDiscoveryCacheEntry(
                pairs=tuple(p for p, _, _ in all_pairs),
                expires_at=now + self._settings.scanner_pair_discovery_ttl_sec,
            )
            self._all_pairs_cache = all_pairs
        else:
            all_pairs = getattr(self, "_all_pairs_cache", ())

        return tuple(
            (pair, base, quote)
            for pair, base, quote in all_pairs
            if base == source_asset or quote == source_asset
        )

    def scan_rotation_candidates(
        self,
        source_asset: str,
        *,
        max_window_hours: float | None = None,
        excluded_assets: frozenset[str] = frozenset(),
    ) -> tuple[RotationCandidate, ...]:
        """Scan for rotation candidates from a given source asset."""
        try:
            asset_pairs = self.discover_asset_pairs(source_asset)
        except PairScannerError as exc:
            logger.warning("Rotation scan aborted for %s: %s", source_asset, exc)
            return ()

        if not asset_pairs:
            return ()

        candidates: list[RotationCandidate] = []
        max_workers = max(1, self._settings.scanner_max_concurrency)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        futures: dict[Future[RotationCandidate | None], str] = {}

        try:
            for pair, base, quote in asset_pairs:
                # Determine destination asset and order side
                if base == source_asset:
                    dest_asset = quote
                    side = OrderSide.SELL  # Sell base to get quote
                else:
                    dest_asset = base
                    side = OrderSide.BUY  # Buy base with quote

                if dest_asset in excluded_assets:
                    continue

                future = executor.submit(
                    self._scan_rotation_pair,
                    pair,
                    source_asset,
                    dest_asset,
                    side,
                    max_window_hours,
                )
                futures[future] = pair

            for future in as_completed(
                futures, timeout=self._settings.scanner_timeout_sec
            ):
                result = future.result()
                if result is not None:
                    candidates.append(result)
        except FuturesTimeoutError:
            logger.warning(
                "Rotation scan timed out for %s (%d candidates found so far)",
                source_asset,
                len(candidates),
            )
            executor.shutdown(wait=False, cancel_futures=True)
        finally:
            if not any(f.cancelled() for f in futures):
                executor.shutdown(wait=True, cancel_futures=False)

        return tuple(
            sorted(
                candidates,
                key=lambda c: (-c.confidence, c.estimated_window_hours, c.pair),
            )
        )

    def _scan_rotation_pair(
        self,
        pair: str,
        source_asset: str,
        dest_asset: str,
        side: OrderSide,
        max_window_hours: float | None,
    ) -> RotationCandidate | None:
        """Evaluate a single pair as a rotation candidate."""
        try:
            bars = self._ohlcv_fetcher(
                pair,
                interval=SCAN_INTERVAL_MINUTES,
                count=max(SCAN_BAR_COUNT, self._technical_source.min_bars),
                timeout=self._settings.scanner_timeout_sec,
            )
        except OHLCVFetchError:
            return None

        if len(bars) < self._technical_source.min_bars:
            return None

        close_f = bars["close"].astype(float)
        vol_f = bars["volume"].astype(float)
        recent_24 = min(24, len(bars))
        usd_volume_24h = float((vol_f.iloc[-recent_24:] * close_f.iloc[-recent_24:]).sum())
        if usd_volume_24h < self._settings.scanner_min_24h_volume_usd:
            logger.debug(
                "Skipped %s: 24h vol $%.0f < $%.0f",
                pair,
                usd_volume_24h,
                self._settings.scanner_min_24h_volume_usd,
            )
            return None

        high_f = bars["high"].astype(float)
        low_f = bars["low"].astype(float)
        recent_6 = min(6, len(bars))
        spread_pct = float(
            (
                (high_f.iloc[-recent_6:] - low_f.iloc[-recent_6:])
                / close_f.iloc[-recent_6:]
            ).mean()
            * 100
        )
        if spread_pct > self._settings.scanner_max_spread_pct:
            logger.debug(
                "Skipped %s: spread %.2f%% > %.2f%%",
                pair,
                spread_pct,
                self._settings.scanner_max_spread_pct,
            )
            return None

        try:
            belief = self._technical_source.analyze(pair, bars)
        except Exception:
            return None

        # 4H trend gate — adjust confidence by alignment with higher timeframe
        if self._settings.mtf_4h_gate_enabled:
            try:
                bars_4h = self._ohlcv_fetcher(
                    pair,
                    interval=240,
                    count=max(SCAN_BAR_COUNT, self._technical_source.min_bars),
                    timeout=self._settings.scanner_timeout_sec,
                )
                if len(bars_4h) >= self._technical_source.min_bars:
                    belief_4h = self._technical_source.analyze(pair, bars_4h)
                    if belief_4h.direction == belief.direction:
                        mtf_factor = self._settings.mtf_aligned_boost
                    elif belief_4h.direction is BeliefDirection.NEUTRAL:
                        mtf_factor = 1.0
                    else:
                        mtf_factor = self._settings.mtf_counter_penalty
                    belief = replace(
                        belief,
                        confidence=min(1.0, belief.confidence * mtf_factor),
                    )
            except Exception:
                pass  # Graceful degradation — use 1H confidence as-is

        # For a BUY rotation (buying dest), we want dest to be bullish
        # For a SELL rotation (selling source for quote), we want source to be bearish
        if side == OrderSide.BUY and belief.direction is not BeliefDirection.BULLISH:
            return None
        if side == OrderSide.SELL and belief.direction is not BeliefDirection.BEARISH:
            return None

        window_hours = _estimate_rotation_window_hours(bars, take_profit_pct=3.0)

        if window_hours <= 0:
            return None
        if max_window_hours is not None and window_hours > max_window_hours:
            return None

        price = Decimal(str(pd.to_numeric(bars["close"], errors="raise").iloc[-1]))
        return RotationCandidate(
            pair=pair,
            from_asset=source_asset,
            to_asset=dest_asset,
            order_side=side,
            confidence=belief.confidence,
            reference_price_hint=price,
            estimated_window_hours=window_hours,
        )


def _normalize_all_spot_pairs(
    raw_pairs: Mapping[str, object],
) -> tuple[tuple[str, str, str], ...]:
    """Normalize all spot pairs. Returns (pair, base, quote) tuples."""
    result: list[tuple[str, str, str]] = []

    for raw_name, metadata in raw_pairs.items():
        if not isinstance(metadata, Mapping):
            continue
        if not _looks_like_any_spot_pair(raw_name, metadata):
            continue

        raw_symbol = _raw_pair_symbol(raw_name, metadata)
        if raw_symbol is None:
            continue

        try:
            normalized_pair = normalize_pair(raw_symbol)
            split = split_normalized_pair(normalized_pair)
        except SymbolNormalizationError:
            continue

        result.append((split.pair, split.base, split.quote))

    return tuple(sorted(set(result)))


def _looks_like_any_spot_pair(raw_name: str, metadata: Mapping[str, object]) -> bool:
    """Check if a raw pair looks like a spot pair (any quote currency)."""
    aclass_base = metadata.get("aclass_base")
    aclass_quote = metadata.get("aclass_quote")
    if aclass_base not in (None, "currency") or aclass_quote not in (None, "currency"):
        return False

    for key in ("wsname", "altname"):
        value = metadata.get(key)
        if isinstance(value, str) and ".d" in value.lower():
            return False
    if ".d" in raw_name.lower():
        return False

    return True


def _fetch_asset_pairs_http(
    client: KrakenClient, timeout_sec: float
) -> Mapping[str, object]:
    prepared = client.get_asset_pairs()
    url = f"{KRAKEN_BASE_URL}{prepared.endpoint}"

    try:
        response = httpx.get(url, params=dict(prepared.payload), timeout=timeout_sec)
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise AssetPairDiscoveryError(
            f"Failed to fetch Kraken AssetPairs: {exc}"
        ) from exc

    errors = data.get("error")
    if errors:
        raise AssetPairDiscoveryError(f"Kraken AssetPairs returned errors: {errors}")

    result = data.get("result")
    if not isinstance(result, Mapping):
        raise AssetPairDiscoveryError(
            "Kraken AssetPairs response missing result mapping."
        )
    return result


def _normalize_usd_spot_pairs(raw_pairs: Mapping[str, object]) -> tuple[str, ...]:
    normalized: set[str] = set()

    for raw_name, metadata in raw_pairs.items():
        if not isinstance(metadata, Mapping):
            continue
        if not _looks_like_spot_pair(raw_name, metadata):
            continue

        raw_symbol = _raw_pair_symbol(raw_name, metadata)
        if raw_symbol is None:
            continue

        try:
            normalized_pair = normalize_pair(raw_symbol)
            split = split_normalized_pair(normalized_pair)
        except SymbolNormalizationError:
            continue

        if split.quote != "USD":
            continue
        normalized.add(split.pair)

    return tuple(sorted(normalized))


def _looks_like_spot_pair(raw_name: str, metadata: Mapping[str, object]) -> bool:
    aclass_base = metadata.get("aclass_base")
    aclass_quote = metadata.get("aclass_quote")
    if aclass_base not in (None, "currency") or aclass_quote not in (None, "currency"):
        return False

    for key in ("wsname", "altname"):
        value = metadata.get(key)
        if isinstance(value, str) and ".d" in value.lower():
            return False
    if ".d" in raw_name.lower():
        return False

    quote = metadata.get("quote")
    if isinstance(quote, str):
        try:
            if normalize_asset_symbol(quote) != "USD":
                return False
        except SymbolNormalizationError:
            return False

    return True


def _raw_pair_symbol(raw_name: str, metadata: Mapping[str, object]) -> str | None:
    for key in ("wsname", "altname"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return raw_name if raw_name.strip() else None


def _estimate_rotation_window_hours(
    bars: pd.DataFrame,
    take_profit_pct: float = 3.0,
) -> float:
    """Estimate hours to reach take-profit based on hourly volatility."""
    close = pd.to_numeric(bars["close"], errors="coerce").astype(float)
    if len(close) < 12:
        return 48.0  # Default to max if insufficient data
    hourly_vol = float(close.pct_change().dropna().std())
    if hourly_vol <= 0 or pd.isna(hourly_vol):
        return 48.0
    hours_to_tp = (take_profit_pct / 100) / hourly_vol
    return max(6.0, min(48.0, hours_to_tp))


def _estimate_bull_peak_hours(bars: pd.DataFrame) -> int:
    close = _coerce_close_series(bars)
    ema_fast = close.ewm(span=EMA_FAST_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
    ema_bullish = bool(ema_fast.iloc[-1] > ema_slow.iloc[-1])

    rsi = _compute_rsi(close)
    rsi_bullish = bool(rsi > RSI_BULLISH_THRESHOLD)

    histogram = _compute_macd_histogram(close)
    macd_bullish = bool(histogram.iloc[-1] > 0.0)

    bullish_count = sum((macd_bullish, rsi_bullish, ema_bullish))
    bucket_index = bullish_count

    if (
        bucket_index > 0
        and macd_bullish
        and _histogram_is_getting_more_positive(histogram)
    ):
        bucket_index = min(bucket_index + 1, len(PEAK_HOUR_BUCKETS) - 1)
    if bucket_index > 0 and rsi > RSI_OVERBOUGHT_THRESHOLD:
        bucket_index = max(bucket_index - 1, 0)

    return PEAK_HOUR_BUCKETS[bucket_index]


def _coerce_close_series(bars: pd.DataFrame) -> pd.Series:
    close = (
        pd.to_numeric(bars["close"], errors="coerce")
        .astype(float)
        .reset_index(drop=True)
    )
    if close.isna().any():
        raise ValueError("close prices must be numeric and non-null")
    if len(close) < EMA_SLOW_SPAN:
        raise ValueError(
            f"Need at least {EMA_SLOW_SPAN} close prices to estimate peak hours."
        )
    return close


def _compute_macd_histogram(close: pd.Series) -> pd.Series:
    ema_fast = close.ewm(span=EMA_FAST_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=MACD_SIGNAL_SPAN, adjust=False).mean()
    return macd_line - signal_line


def _compute_rsi(close: pd.Series) -> float:
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = float(
        gains.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean().iloc[-1]
    )
    avg_loss = float(
        losses.rolling(window=RSI_PERIOD, min_periods=RSI_PERIOD).mean().iloc[-1]
    )

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0

    relative_strength = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + relative_strength))


def _histogram_is_getting_more_positive(histogram: pd.Series) -> bool:
    return bool(histogram.iloc[-1] > histogram.iloc[-2] > 0.0)


def evaluate_root_ta(
    bars: pd.DataFrame,
) -> tuple[str, float, float]:
    """Evaluate TA on OHLCV bars and return (direction, window_hours, confidence).

    direction: "bullish", "bearish", or "neutral"
    window_hours: estimated hours to TP, clamped [2, 48]
    confidence: signal agreement strength (0.0 to 1.0)

    Uses the same EMA/RSI/MACD signals as the rotation scanner.
    """
    close = _coerce_close_series(bars)
    ema_fast = close.ewm(span=EMA_FAST_SPAN, adjust=False).mean()
    ema_slow = close.ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
    ema_bullish = bool(ema_fast.iloc[-1] > ema_slow.iloc[-1])

    rsi = _compute_rsi(close)
    rsi_bullish = bool(rsi > RSI_BULLISH_THRESHOLD)

    histogram = _compute_macd_histogram(close)
    macd_bullish = bool(histogram.iloc[-1] > 0.0)

    bullish_count = sum((ema_bullish, rsi_bullish, macd_bullish))
    if bullish_count >= 2:
        direction = "bullish"
        confidence = bullish_count / 3.0
    elif bullish_count == 0:
        direction = "bearish"
        confidence = 1.0  # all 3 signals agree on bearish
    else:
        direction = "neutral"
        confidence = 1.0 / 3.0  # only 1 signal, ambiguous

    window_hours = _estimate_rotation_window_hours(bars, take_profit_pct=3.0)
    return direction, window_hours, confidence


# Assets that ARE quote currencies — never set exit windows on these
QUOTE_ASSETS: frozenset[str] = frozenset(
    {
        "USD",
        "USDT",
        "USDC",
        "EUR",
        "GBP",
        "CAD",
        "AUD",
        "JPY",
        "CHF",
    }
)

# Preferred quote currencies for root exit (best first)
PREFERRED_QUOTES: tuple[str, ...] = ("USD", "USDT", "USDC", "EUR")


__all__ = [
    "AssetPairDiscoveryError",
    "PairScanner",
    "PairScannerError",
    "PREFERRED_QUOTES",
    "QUOTE_ASSETS",
    "evaluate_root_ta",
]
