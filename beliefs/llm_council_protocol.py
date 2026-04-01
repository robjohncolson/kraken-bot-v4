"""LLM Council message protocol — shared between handler, broker, and agents."""

from __future__ import annotations

import json
import logging
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
            indent=2, default=str,
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
            indent=2, default=str,
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
            direction=data.get("direction", data.get("consensus", {}).get("direction", "neutral")),
            confidence=float(data.get("confidence", data.get("consensus", {}).get("confidence", 0.0))),
            regime=data.get("regime", data.get("consensus", {}).get("regime", "unknown")),
            completed_at=data.get("completed_at", ""),
            valid_vote_count=int(data.get("valid_vote_count", len(data.get("votes", {})))),
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
    Split (2 agents disagree): returns neutral/0.0 (safe for direction-based trading).
    """
    valid = [v for v in votes if v.direction in ("bullish", "bearish", "neutral")]
    if not valid:
        return "neutral", 0.0, "unknown"

    directions = [v.direction for v in valid]
    if len(set(directions)) == 1:
        # Unanimous (includes single-agent case)
        avg_conf = sum(v.confidence for v in valid) / len(valid)
        regime = valid[0].regime
        return directions[0], round(avg_conf, 4), regime

    # Split — neutral (safe: direction-based trading would act on any non-neutral)
    return "neutral", 0.0, "unknown"


def council_paths(base_dir: str | Path = COUNCIL_DIR) -> dict[str, Path]:
    base = Path(base_dir)
    return {
        "requests": base / "requests",
        "responses": base / "responses",
        "consensus": base / "consensus",
    }
