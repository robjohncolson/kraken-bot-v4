"""Tests for LLM Council protocol, handler, and consensus."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from beliefs.llm_council_protocol import (
    CouncilConsensus,
    CouncilRequest,
    CouncilVote,
    compute_consensus,
    make_call_id,
)
from core.types import BeliefDirection, BeliefSource


def test_make_call_id_format() -> None:
    now = datetime(2026, 3, 31, 14, 0, 0, tzinfo=timezone.utc)
    call_id = make_call_id("DOGE/USD", now)
    assert call_id == "2026-03-31T14-00-00Z-dogeusd"


def test_council_request_roundtrip() -> None:
    request = CouncilRequest(
        call_id="test-1",
        pair="DOGE/USD",
        as_of="2026-03-31T14:00:00+00:00",
        context={"price": 0.09, "rsi": 45.0},
    )
    raw = request.to_json()
    restored = CouncilRequest.from_json(raw)
    assert restored.call_id == "test-1"
    assert restored.pair == "DOGE/USD"
    assert restored.context["price"] == 0.09


def test_council_vote_from_json() -> None:
    raw = json.dumps({
        "agent": "claude",
        "direction": "bullish",
        "confidence": 0.72,
        "regime": "trending",
        "reasoning": "RSI rising",
    })
    vote = CouncilVote.from_json(raw)
    assert vote.agent == "claude"
    assert vote.direction == "bullish"
    assert vote.confidence == 0.72


def test_consensus_unanimous_bullish() -> None:
    votes = [
        CouncilVote("claude", "bullish", 0.8, "trending"),
        CouncilVote("codex", "bullish", 0.6, "trending"),
    ]
    direction, confidence, regime = compute_consensus(votes)
    assert direction == "bullish"
    assert confidence == 0.7
    assert regime == "trending"


def test_consensus_split_is_neutral() -> None:
    votes = [
        CouncilVote("claude", "bullish", 0.8, "trending"),
        CouncilVote("codex", "bearish", 0.7, "ranging"),
    ]
    direction, confidence, regime = compute_consensus(votes)
    assert direction == "neutral"
    assert confidence == 0.0


def test_consensus_empty_is_neutral() -> None:
    direction, confidence, regime = compute_consensus([])
    assert direction == "neutral"
    assert confidence == 0.0


def test_consensus_roundtrip() -> None:
    consensus = CouncilConsensus(
        call_id="test-1",
        pair="DOGE/USD",
        as_of="2026-03-31T14:00:00+00:00",
        status="completed",
        votes={"claude": {"direction": "bullish"}, "codex": {"direction": "bullish"}},
        direction="bullish",
        confidence=0.7,
        regime="trending",
        completed_at="2026-03-31T14:00:15+00:00",
    )
    raw = consensus.to_json()
    restored = CouncilConsensus.from_json(raw)
    assert restored.direction == "bullish"
    assert restored.confidence == 0.7


def test_handler_writes_request_file(tmp_path: Path) -> None:
    from beliefs.llm_council_handler import make_llm_council_handler
    from unittest.mock import patch

    handler = make_llm_council_handler(council_dir=tmp_path)

    # Mock OHLCV fetch
    import pandas as pd
    import numpy as np
    mock_bars = pd.DataFrame({
        "open": np.ones(50), "high": np.ones(50),
        "low": np.ones(50), "close": np.ones(50) * 0.09,
        "volume": np.ones(50) * 100,
    })
    with patch("beliefs.llm_council_handler.fetch_ohlcv", return_value=mock_bars):
        from scheduler import BeliefRefreshRequest
        request = BeliefRefreshRequest(
            pair="DOGE/USD",
            position_id="",
            checked_at=datetime(2026, 3, 31, 14, 0, tzinfo=timezone.utc),
            stale_after_hours=4,
        )
        result = handler(request)

    # Should return None (no consensus yet) and write a request file
    assert result is None
    requests = list((tmp_path / "requests").glob("*.json"))
    assert len(requests) == 1


def test_handler_returns_belief_from_cached_consensus(tmp_path: Path) -> None:
    from beliefs.llm_council_handler import make_llm_council_handler

    handler = make_llm_council_handler(council_dir=tmp_path)

    # Write a fresh consensus file
    now = datetime.now(timezone.utc)
    call_id = make_call_id("DOGE/USD", now)
    consensus = CouncilConsensus(
        call_id=call_id,
        pair="DOGE/USD",
        as_of=now.isoformat(),
        status="completed",
        votes={"claude": {"direction": "bullish"}, "codex": {"direction": "bullish"}},
        direction="bullish",
        confidence=0.7,
        regime="trending",
    )
    consensus_dir = tmp_path / "consensus"
    consensus_dir.mkdir(parents=True, exist_ok=True)
    (consensus_dir / f"{call_id}.json").write_text(consensus.to_json())

    from scheduler import BeliefRefreshRequest
    request = BeliefRefreshRequest(
        pair="DOGE/USD",
        position_id="",
        checked_at=now,
        stale_after_hours=4,
    )
    result = handler(request)

    assert result is not None
    assert result.direction == BeliefDirection.BULLISH
    assert result.confidence == 0.7
    assert BeliefSource.LLM_COUNCIL in result.sources
