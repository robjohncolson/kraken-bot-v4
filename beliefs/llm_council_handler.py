"""LLM Council belief handler — runtime-facing interface.

Builds market context, writes request files, reads cached consensus.
Does NOT call LLMs directly — the sidecar broker handles transport.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

import pandas as pd

from beliefs.llm_council_protocol import (
    COUNCIL_DIR,
    CouncilConsensus,
    CouncilRequest,
    council_paths,
    make_call_id,
)
from core.types import (
    BeliefDirection,
    BeliefSnapshot,
    BeliefSource,
    MarketRegime,
)
from exchange.ohlcv import OHLCVFetchError, fetch_ohlcv
from scheduler import BeliefRefreshRequest

logger = logging.getLogger(__name__)

STALE_SECONDS: Final[int] = 3600  # Consider consensus stale after 1 hour
OHLCV_BARS: Final[int] = 50


def make_llm_council_handler(
    council_dir: str | Path = COUNCIL_DIR,
    artifact_source: object | None = None,
) -> callable:
    """Create a belief handler backed by the LLM council."""
    paths = council_paths(council_dir)
    for p in paths.values():
        p.mkdir(parents=True, exist_ok=True)

    def handler(request: BeliefRefreshRequest) -> BeliefSnapshot | None:
        pair = request.pair
        now = datetime.now(timezone.utc)

        # Check for fresh cached consensus
        consensus = _find_fresh_consensus(paths["consensus"], pair, now)
        if consensus is not None:
            return _consensus_to_belief(consensus)

        # No fresh consensus — write a request for the broker
        _write_request(paths["requests"], pair, now, artifact_source, paths["consensus"])
        logger.info("LLM council request written for %s (awaiting broker)", pair)
        return None

    return handler


def make_fallback_council_handler(
    council_dir: str | Path = COUNCIL_DIR,
    artifact_source: object | None = None,
    fallback_handler: callable | None = None,
) -> callable:
    """Council handler with automatic fallback to technical_ensemble.

    First poll: council has no consensus → writes request → falls back to ensemble.
    Subsequent polls: council finds fresh consensus → returns council belief.
    Bot is never belief-less (unless OHLCV is also down).
    """
    council = make_llm_council_handler(council_dir, artifact_source)

    if fallback_handler is None:
        from beliefs.technical_ensemble_handler import technical_ensemble_belief_handler
        fallback_handler = technical_ensemble_belief_handler

    def handler(request: BeliefRefreshRequest) -> BeliefSnapshot | None:
        belief = council(request)
        if belief is not None:
            return belief
        # Council wrote a request for the broker; produce an instant fallback
        logger.info("Council unavailable for %s, falling back to technical_ensemble", request.pair)
        return fallback_handler(request)

    return handler


_ACCEPTED_STATUSES: Final[frozenset[str]] = frozenset({"completed", "partial"})


def _find_fresh_consensus(
    consensus_dir: Path, pair: str, now: datetime,
) -> CouncilConsensus | None:
    """Find the newest valid consensus for a pair within staleness window."""
    slug = pair.replace("/", "").lower()
    candidates = sorted(consensus_dir.glob(f"*-{slug}.json"), reverse=True)

    for path in candidates[:5]:  # Check last 5 at most
        try:
            raw = path.read_text(encoding="utf-8")
            consensus = CouncilConsensus.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError):
            continue

        if consensus.pair != pair or consensus.status not in _ACCEPTED_STATUSES:
            continue

        # Check staleness
        try:
            as_of = datetime.fromisoformat(consensus.as_of.replace("Z", "+00:00"))
            age = (now - as_of).total_seconds()
            if age <= STALE_SECONDS:
                return consensus
        except ValueError:
            continue

    return None


def _write_request(
    requests_dir: Path,
    pair: str,
    now: datetime,
    artifact_source: object | None,
    consensus_dir: Path | None = None,
) -> None:
    """Build market context and write a council request file.

    Skips if an unprocessed request for this pair already exists (backoff).
    """
    call_id = make_call_id(pair, now)
    request_path = requests_dir / f"{call_id}.json"

    if request_path.exists():
        return  # Already requested this slot

    # Per-pair backoff: skip if a pending request already exists for this pair
    slug = pair.replace("/", "").lower()
    for existing in requests_dir.glob(f"*-{slug}.json"):
        if consensus_dir is not None and (consensus_dir / existing.name).exists():
            continue  # This request was already processed
        if existing != request_path:
            logger.debug("Skipping duplicate request for %s (pending: %s)", pair, existing.name)
            return

    context = _build_market_context(pair, artifact_source)
    request = CouncilRequest(
        call_id=call_id,
        pair=pair,
        as_of=now.isoformat(),
        context=context,
    )
    request_path.write_text(request.to_json(), encoding="utf-8")


def _build_market_context(pair: str, artifact_source: object | None) -> dict:
    """Build structured market context from available data sources."""
    context: dict = {"pair": pair}

    # OHLCV
    try:
        bars = fetch_ohlcv(pair, interval=60, count=OHLCV_BARS)
        if not bars.empty:
            close = pd.to_numeric(bars["close"], errors="coerce").astype(float)
            context["price"] = float(close.iloc[-1])
            if len(close) >= 2:
                context["change_1h_pct"] = round((close.iloc[-1] / close.iloc[-2] - 1) * 100, 3)
            if len(close) >= 4:
                context["change_4h_pct"] = round((close.iloc[-1] / close.iloc[-4] - 1) * 100, 3)
            if len(close) >= 24:
                context["change_24h_pct"] = round((close.iloc[-1] / close.iloc[-24] - 1) * 100, 3)

            # Basic technicals
            if len(close) >= 26:
                ema_fast = close.ewm(span=7, adjust=False).mean()
                ema_slow = close.ewm(span=26, adjust=False).mean()
                context["ema_crossover_bullish"] = bool(ema_fast.iloc[-1] > ema_slow.iloc[-1])

            if len(close) >= 14:
                delta = close.diff()
                gains = delta.clip(lower=0.0)
                losses = -delta.clip(upper=0.0)
                avg_gain = float(gains.rolling(14).mean().iloc[-1])
                avg_loss = float(losses.rolling(14).mean().iloc[-1])
                rsi = 50.0
                if avg_loss > 0:
                    rsi = 100 - 100 / (1 + avg_gain / avg_loss)
                elif avg_gain > 0:
                    rsi = 100.0
                context["rsi_14"] = round(rsi, 2)

            if len(close) >= 12:
                context["volatility_12h"] = round(float(close.pct_change().rolling(12).std().iloc[-1]), 6)
    except OHLCVFetchError as exc:
        logger.warning("LLM council: OHLCV fetch failed for %s: %s", pair, exc)

    # V1 model output if available
    if artifact_source is not None:
        try:
            prob_up = artifact_source.predict_raw(bars)
            context["v1_prob_up"] = round(float(prob_up), 4)
        except Exception:
            pass

    return context


def _consensus_to_belief(consensus: CouncilConsensus) -> BeliefSnapshot:
    """Convert a council consensus to a BeliefSnapshot.

    Partial consensus (fewer valid votes than expected) gets coverage-based
    confidence scaling: confidence * (valid_votes / expected_votes).
    """
    direction_map = {
        "bullish": BeliefDirection.BULLISH,
        "bearish": BeliefDirection.BEARISH,
    }
    regime_map = {
        "trending": MarketRegime.TRENDING,
        "ranging": MarketRegime.RANGING,
    }
    confidence = consensus.confidence
    if consensus.status == "partial" and consensus.expected_vote_count > 0:
        coverage = consensus.valid_vote_count / consensus.expected_vote_count
        confidence *= coverage
    return BeliefSnapshot(
        pair=consensus.pair,
        direction=direction_map.get(consensus.direction, BeliefDirection.NEUTRAL),
        confidence=round(confidence, 2),
        regime=regime_map.get(consensus.regime, MarketRegime.UNKNOWN),
        sources=(BeliefSource.LLM_COUNCIL,),
    )


__all__ = ["make_llm_council_handler", "make_fallback_council_handler"]
