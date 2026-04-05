"""LLM Council message protocol — shared between handler, broker, and agents."""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

SCHEMA_VERSION: Final[str] = "llm-council/v1"
COUNCIL_DIR: Final[str] = "state/llm-council"


@dataclass(frozen=True, slots=True)
class CouncilRequest:
    call_id: str
    pair: str
    as_of: str
    context: dict

    def to_json(self) -> str:
        return json.dumps(
            {"schema_version": SCHEMA_VERSION, **asdict(self)},
            indent=2,
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str) -> CouncilRequest:
        data = json.loads(raw)
        return cls(
            call_id=data["call_id"],
            pair=data["pair"],
            as_of=data["as_of"],
            context=data["context"],
        )


@dataclass(frozen=True, slots=True)
class CouncilVote:
    agent: str
    direction: str  # bullish, bearish, neutral
    confidence: float
    regime: str = "unknown"
    reasoning: str = ""

    @classmethod
    def from_json(cls, raw: str) -> CouncilVote:
        data = json.loads(raw)
        return cls(
            agent=data.get("agent", "unknown"),
            direction=data.get("direction", "neutral"),
            confidence=float(data.get("confidence", 0.0)),
            regime=data.get("regime", "unknown"),
            reasoning=data.get("reasoning", ""),
        )


@dataclass(frozen=True, slots=True)
class CouncilConsensus:
    call_id: str
    pair: str
    as_of: str
    status: str  # completed, partial, failed
    votes: dict  # agent -> {direction, confidence, regime}
    direction: str
    confidence: float
    regime: str = "unknown"
    completed_at: str = ""
    valid_vote_count: int = 0
    expected_vote_count: int = 2

    def to_json(self) -> str:
        return json.dumps(
            {"schema_version": SCHEMA_VERSION, **asdict(self)},
            indent=2,
            default=str,
        )

    @classmethod
    def from_json(cls, raw: str) -> CouncilConsensus:
        data = json.loads(raw)
        return cls(
            call_id=data["call_id"],
            pair=data["pair"],
            as_of=data["as_of"],
            status=data.get("status", "completed"),
            votes=data.get("votes", {}),
            direction=data.get(
                "direction", data.get("consensus", {}).get("direction", "neutral")
            ),
            confidence=float(
                data.get("confidence", data.get("consensus", {}).get("confidence", 0.0))
            ),
            regime=data.get(
                "regime", data.get("consensus", {}).get("regime", "unknown")
            ),
            completed_at=data.get("completed_at", ""),
            valid_vote_count=int(
                data.get("valid_vote_count", len(data.get("votes", {})))
            ),
            expected_vote_count=int(data.get("expected_vote_count", 2)),
        )


def make_call_id(pair: str, now: datetime | None = None) -> str:
    ts = now or datetime.now(timezone.utc)
    slug = pair.replace("/", "").lower()
    return f"{ts.strftime('%Y-%m-%dT%H-%M-%SZ')}-{slug}"


def compute_consensus(votes: list[CouncilVote]) -> tuple[str, float, str]:
    """Majority vote from council votes. Returns (direction, confidence, regime).

    Single-agent vote: returns that direction at that confidence.
    Unanimous multi-agent: returns shared direction at average confidence.
    Majority disagreement: returns the majority direction at scaled confidence.
    Perfect splits remain neutral/0.0.
    """
    valid = [v for v in votes if v.direction in ("bullish", "bearish", "neutral")]
    if not valid:
        return "neutral", 0.0, "unknown"

    direction_counts = Counter(v.direction for v in valid)
    majority_count = max(direction_counts.values())
    winners = [
        direction
        for direction, count in direction_counts.items()
        if count == majority_count
    ]
    if len(winners) != 1 or (majority_count * 2) <= len(valid):
        return "neutral", 0.0, "unknown"

    winning_direction = winners[0]
    winning_votes = [vote for vote in valid if vote.direction == winning_direction]
    avg_confidence = sum(v.confidence for v in winning_votes) / len(winning_votes)
    if majority_count == len(valid):
        confidence = avg_confidence
    else:
        confidence = avg_confidence * (majority_count / len(valid))
    return (
        winning_direction,
        round(confidence, 4),
        _majority_regime(winning_votes),
    )


def _majority_regime(votes: list[CouncilVote]) -> str:
    regimes = [vote.regime for vote in votes if vote.regime]
    if not regimes:
        return "unknown"
    regime_counts = Counter(regimes)
    majority_count = max(regime_counts.values())
    winners = [
        regime for regime, count in regime_counts.items() if count == majority_count
    ]
    if len(winners) != 1:
        return "unknown"
    return winners[0]


def council_paths(base_dir: str | Path = COUNCIL_DIR) -> dict[str, Path]:
    base = Path(base_dir)
    return {
        "requests": base / "requests",
        "responses": base / "responses",
        "consensus": base / "consensus",
    }
