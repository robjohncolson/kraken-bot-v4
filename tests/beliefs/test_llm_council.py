"""Tests for LLM Council protocol, handler, broker helpers, and consensus."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from beliefs.llm_council_protocol import (
    CouncilConsensus,
    CouncilRequest,
    CouncilVote,
    compute_consensus,
    make_call_id,
)
from core.types import BeliefDirection, BeliefSource
from scheduler import BeliefRefreshRequest


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


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
    raw = json.dumps(
        {
            "agent": "claude",
            "direction": "bullish",
            "confidence": 0.72,
            "regime": "trending",
            "reasoning": "RSI rising",
        }
    )
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


def test_consensus_two_of_three_majority_scales_confidence() -> None:
    votes = [
        CouncilVote("claude", "bullish", 0.8, "trending"),
        CouncilVote("codex", "bullish", 0.6, "trending"),
        CouncilVote("gemini", "bearish", 0.7, "ranging"),
    ]
    direction, confidence, regime = compute_consensus(votes)
    assert direction == "bullish"
    assert confidence == 0.4667
    assert regime == "trending"


def test_consensus_three_way_split_is_neutral() -> None:
    votes = [
        CouncilVote("claude", "bullish", 0.8, "trending"),
        CouncilVote("codex", "bearish", 0.7, "ranging"),
        CouncilVote("gemini", "neutral", 0.6, "unknown"),
    ]
    direction, confidence, regime = compute_consensus(votes)
    assert direction == "neutral"
    assert confidence == 0.0
    assert regime == "unknown"


def test_consensus_empty_is_neutral() -> None:
    direction, confidence, regime = compute_consensus([])
    assert direction == "neutral"
    assert confidence == 0.0


def test_consensus_single_vote() -> None:
    """Single-agent vote returns that agent's direction at full confidence."""
    votes = [CouncilVote("claude", "bearish", 0.75, "trending")]
    direction, confidence, regime = compute_consensus(votes)
    assert direction == "bearish"
    assert confidence == 0.75
    assert regime == "trending"


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
        valid_vote_count=2,
        expected_vote_count=2,
    )
    raw = consensus.to_json()
    restored = CouncilConsensus.from_json(raw)
    assert restored.direction == "bullish"
    assert restored.confidence == 0.7
    assert restored.valid_vote_count == 2
    assert restored.expected_vote_count == 2


def test_consensus_roundtrip_with_vote_counts() -> None:
    """New fields valid_vote_count/expected_vote_count roundtrip correctly."""
    consensus = CouncilConsensus(
        call_id="test-partial",
        pair="DOGE/USD",
        as_of="2026-03-31T14:00:00+00:00",
        status="partial",
        votes={
            "claude": {"direction": "bearish", "confidence": 0.6, "regime": "trending"}
        },
        direction="bearish",
        confidence=0.6,
        regime="trending",
        valid_vote_count=1,
        expected_vote_count=2,
    )
    raw = consensus.to_json()
    restored = CouncilConsensus.from_json(raw)
    assert restored.valid_vote_count == 1
    assert restored.expected_vote_count == 2
    assert restored.status == "partial"


def test_consensus_backward_compat_no_vote_counts() -> None:
    """Old consensus files without vote count fields still parse."""
    raw = json.dumps(
        {
            "schema_version": "llm-council/v1",
            "call_id": "old-1",
            "pair": "DOGE/USD",
            "as_of": "2026-03-31T14:00:00+00:00",
            "status": "completed",
            "votes": {
                "claude": {"direction": "bullish"},
                "codex": {"direction": "bullish"},
            },
            "direction": "bullish",
            "confidence": 0.7,
            "regime": "trending",
        }
    )
    restored = CouncilConsensus.from_json(raw)
    assert restored.valid_vote_count == 2  # defaults to len(votes)
    assert restored.expected_vote_count == 2


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------


def _mock_bars() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": np.ones(50),
            "high": np.ones(50),
            "low": np.ones(50),
            "close": np.ones(50) * 0.09,
            "volume": np.ones(50) * 100,
        }
    )


def _make_request(
    pair: str = "DOGE/USD", now: datetime | None = None
) -> BeliefRefreshRequest:
    return BeliefRefreshRequest(
        pair=pair,
        position_id="",
        checked_at=now or datetime.now(timezone.utc),
        stale_after_hours=4,
    )


def test_handler_writes_request_file(tmp_path: Path) -> None:
    from beliefs.llm_council_handler import make_llm_council_handler

    handler = make_llm_council_handler(council_dir=tmp_path)
    with patch("beliefs.llm_council_handler.fetch_ohlcv", return_value=_mock_bars()):
        result = handler(_make_request())

    assert result is None
    requests = list((tmp_path / "requests").glob("*.json"))
    assert len(requests) == 1


def test_handler_returns_belief_from_cached_consensus(tmp_path: Path) -> None:
    from beliefs.llm_council_handler import make_llm_council_handler

    handler = make_llm_council_handler(council_dir=tmp_path)

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

    result = handler(_make_request(now=now))

    assert result is not None
    assert result.direction == BeliefDirection.BULLISH
    assert result.confidence == 0.7
    assert BeliefSource.LLM_COUNCIL in result.sources


def test_handler_fallback_to_technical_ensemble(tmp_path: Path) -> None:
    """When council has no consensus, fallback handler returns ensemble belief."""
    from beliefs.llm_council_handler import make_fallback_council_handler
    from core.types import BeliefSnapshot, MarketRegime

    mock_belief = BeliefSnapshot(
        pair="DOGE/USD",
        direction=BeliefDirection.BEARISH,
        confidence=0.6,
        regime=MarketRegime.TRENDING,
        sources=(BeliefSource.TECHNICAL_ENSEMBLE,),
    )
    mock_fallback = lambda req: mock_belief  # noqa: E731

    handler = make_fallback_council_handler(
        council_dir=tmp_path, fallback_handler=mock_fallback
    )
    with patch("beliefs.llm_council_handler.fetch_ohlcv", return_value=_mock_bars()):
        result = handler(_make_request())

    assert result is not None
    assert result.direction == BeliefDirection.BEARISH
    assert BeliefSource.TECHNICAL_ENSEMBLE in result.sources


def test_handler_council_wins_over_fallback(tmp_path: Path) -> None:
    """When council has fresh consensus, fallback is not called."""
    from beliefs.llm_council_handler import make_fallback_council_handler

    # Write fresh consensus
    now = datetime.now(timezone.utc)
    call_id = make_call_id("DOGE/USD", now)
    consensus = CouncilConsensus(
        call_id=call_id,
        pair="DOGE/USD",
        as_of=now.isoformat(),
        status="completed",
        votes={"claude": {"direction": "bullish"}, "codex": {"direction": "bullish"}},
        direction="bullish",
        confidence=0.8,
        regime="trending",
    )
    consensus_dir = tmp_path / "consensus"
    consensus_dir.mkdir(parents=True, exist_ok=True)
    (consensus_dir / f"{call_id}.json").write_text(consensus.to_json())

    fallback_called = False

    def mock_fallback(req):
        nonlocal fallback_called
        fallback_called = True
        return None

    handler = make_fallback_council_handler(
        council_dir=tmp_path, fallback_handler=mock_fallback
    )
    result = handler(_make_request(now=now))

    assert result is not None
    assert result.direction == BeliefDirection.BULLISH
    assert BeliefSource.LLM_COUNCIL in result.sources
    assert not fallback_called


def test_handler_accepts_partial_consensus(tmp_path: Path) -> None:
    """Partial consensus (1/2 agents) produces belief with reduced confidence."""
    from beliefs.llm_council_handler import make_llm_council_handler

    handler = make_llm_council_handler(council_dir=tmp_path)

    now = datetime.now(timezone.utc)
    call_id = make_call_id("DOGE/USD", now)
    consensus = CouncilConsensus(
        call_id=call_id,
        pair="DOGE/USD",
        as_of=now.isoformat(),
        status="partial",
        votes={
            "claude": {"direction": "bearish", "confidence": 0.8, "regime": "trending"}
        },
        direction="bearish",
        confidence=0.8,
        regime="trending",
        valid_vote_count=1,
        expected_vote_count=2,
    )
    consensus_dir = tmp_path / "consensus"
    consensus_dir.mkdir(parents=True, exist_ok=True)
    (consensus_dir / f"{call_id}.json").write_text(consensus.to_json())

    result = handler(_make_request(now=now))

    assert result is not None
    assert result.direction == BeliefDirection.BEARISH
    # Coverage scaling: 0.8 * (1/2) = 0.4
    assert result.confidence == 0.4
    assert BeliefSource.LLM_COUNCIL in result.sources


def test_handler_rejects_failed_consensus(tmp_path: Path) -> None:
    """Failed consensus (0 valid votes) is not accepted."""
    from beliefs.llm_council_handler import make_llm_council_handler

    handler = make_llm_council_handler(council_dir=tmp_path)

    now = datetime.now(timezone.utc)
    call_id = make_call_id("DOGE/USD", now)
    consensus = CouncilConsensus(
        call_id=call_id,
        pair="DOGE/USD",
        as_of=now.isoformat(),
        status="failed",
        votes={},
        direction="neutral",
        confidence=0.0,
        valid_vote_count=0,
        expected_vote_count=2,
    )
    consensus_dir = tmp_path / "consensus"
    consensus_dir.mkdir(parents=True, exist_ok=True)
    (consensus_dir / f"{call_id}.json").write_text(consensus.to_json())

    with patch("beliefs.llm_council_handler.fetch_ohlcv", return_value=_mock_bars()):
        result = handler(_make_request(now=now))

    # Failed consensus is rejected — handler writes new request, returns None
    assert result is None


def test_request_backoff_skips_duplicate(tmp_path: Path) -> None:
    """Second call for same pair skips if a pending request already exists."""
    from beliefs.llm_council_handler import make_llm_council_handler

    handler = make_llm_council_handler(council_dir=tmp_path)

    with patch("beliefs.llm_council_handler.fetch_ohlcv", return_value=_mock_bars()):
        handler(_make_request())
        # Second call should not create another request file
        handler(_make_request())

    requests = list((tmp_path / "requests").glob("*-dogeusd.json"))
    assert len(requests) == 1


# ---------------------------------------------------------------------------
# Broker helper tests
# ---------------------------------------------------------------------------


def test_cleanup_stale_files(tmp_path: Path) -> None:
    from scripts.llm_council_broker import _cleanup_stale_files

    paths = {
        "requests": tmp_path / "requests",
        "responses": tmp_path / "responses",
        "consensus": tmp_path / "consensus",
    }
    for p in paths.values():
        p.mkdir()

    # Create an old file and a fresh file
    old_file = paths["requests"] / "old-request.json"
    old_file.write_text("{}")
    # Set mtime to 48 hours ago
    import os

    old_mtime = time.time() - 48 * 3600
    os.utime(old_file, (old_mtime, old_mtime))

    fresh_file = paths["requests"] / "fresh-request.json"
    fresh_file.write_text("{}")

    removed = _cleanup_stale_files(paths, max_age_hours=24)

    assert removed == 1
    assert not old_file.exists()
    assert fresh_file.exists()


def test_pane_exists_returns_false_when_tmux_missing() -> None:
    from scripts.llm_council_broker import _pane_exists

    with patch(
        "scripts.llm_council_broker.subprocess.run", side_effect=FileNotFoundError
    ):
        assert _pane_exists("work:2.0") is False


def test_pane_is_ready_returns_false_when_no_pane() -> None:
    from scripts.llm_council_broker import _pane_is_ready

    with patch("scripts.llm_council_broker._pane_exists", return_value=False):
        assert _pane_is_ready("work:2.0") is False
