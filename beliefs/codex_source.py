from __future__ import annotations

import json
import math
import re
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any

from beliefs.prompts import build_belief_prompt
from core.types import BeliefDirection, BeliefSnapshot, BeliefSource, MarketRegime

DEFAULT_TIMEFRAME = "1h"
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_CLOSED_POSITION_LOOKBACK = 5

_DIRECTION_PATTERN = re.compile(
    r"(?im)^[\s>*-]*direction\s*[:=]\s*(bullish|bearish|neutral)\b"
)
_CONFIDENCE_PATTERN = re.compile(
    r"(?im)^[\s>*-]*confidence\s*[:=]\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\b"
)
_REGIME_PATTERN = re.compile(
    r"(?im)^[\s>*-]*regime\s*[:=]\s*(trending|ranging|unknown)\b"
)


class CodexSourceError(ValueError):
    """Base exception for Codex belief source configuration errors."""


class InvalidCodexTimeoutError(CodexSourceError):
    """Raised when the configured CLI timeout is not positive."""

    def __init__(self, timeout_seconds: int) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"timeout_seconds must be positive; got {timeout_seconds}.")


class InvalidCodexLookbackError(CodexSourceError):
    """Raised when the configured closed-position lookback is not positive."""

    def __init__(self, lookback: int) -> None:
        self.lookback = lookback
        super().__init__(f"last_n_closed_positions must be positive; got {lookback}.")


class CodexSource:
    """Adapter for forming a belief through the Codex CLI."""

    def __init__(
        self,
        *,
        timeframe: str = DEFAULT_TIMEFRAME,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        last_n_closed_positions: int = DEFAULT_CLOSED_POSITION_LOOKBACK,
    ) -> None:
        if timeout_seconds < 1:
            raise InvalidCodexTimeoutError(timeout_seconds)
        if last_n_closed_positions < 1:
            raise InvalidCodexLookbackError(last_n_closed_positions)

        self.timeframe = timeframe
        self.timeout_seconds = timeout_seconds
        self.last_n_closed_positions = last_n_closed_positions

    def analyze(
        self,
        pair: str,
        recent_trade_history_summary: str,
        *,
        timeframe: str | None = None,
    ) -> BeliefSnapshot | None:
        prompt = build_belief_prompt(
            pair=pair,
            timeframe=timeframe or self.timeframe,
            recent_trade_history_summary=recent_trade_history_summary,
            last_n_closed_positions=self.last_n_closed_positions,
        )

        result = self._run(prompt)
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return None

        return self.parse_response(pair=pair, raw_output=result.stdout)

    def parse_response(self, *, pair: str, raw_output: str) -> BeliefSnapshot | None:
        payload = self._extract_payload(raw_output)
        if payload is None:
            return None

        try:
            direction = BeliefDirection(str(payload["direction"]).lower())
            confidence = float(payload["confidence"])
            regime = MarketRegime(str(payload["regime"]).lower())
        except (KeyError, TypeError, ValueError):
            return None

        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            return None

        return BeliefSnapshot(
            pair=pair,
            direction=direction,
            confidence=confidence,
            regime=regime,
            sources=(BeliefSource.CODEX,),
        )

    def _run(self, prompt: str) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                ["codex", "exec", "--full-auto", "-"],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout_seconds,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            return None

    def _extract_payload(self, raw_output: str) -> Mapping[str, Any] | None:
        payload = self._extract_payload_object(self._decode_json_text(raw_output))
        if payload is not None:
            return payload
        return self._extract_labeled_payload(raw_output)

    def _extract_payload_object(self, value: object) -> Mapping[str, Any] | None:
        if isinstance(value, Mapping):
            if self._is_belief_payload(value):
                return value
            for nested_value in value.values():
                payload = self._extract_payload_object(nested_value)
                if payload is not None:
                    return payload
            return None

        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for item in value:
                payload = self._extract_payload_object(item)
                if payload is not None:
                    return payload
            return None

        if isinstance(value, str):
            return self._extract_payload_object(self._decode_json_text(value))

        return None

    def _extract_labeled_payload(self, text: str) -> Mapping[str, str] | None:
        direction_match = _DIRECTION_PATTERN.search(text)
        confidence_match = _CONFIDENCE_PATTERN.search(text)
        regime_match = _REGIME_PATTERN.search(text)

        if direction_match is None or confidence_match is None or regime_match is None:
            return None

        return {
            "direction": direction_match.group(1).lower(),
            "confidence": confidence_match.group(1),
            "regime": regime_match.group(1).lower(),
        }

    def _decode_json_text(self, text: str) -> object | None:
        stripped = text.strip()
        if not stripped:
            return None

        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                return None
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None

    @staticmethod
    def _is_belief_payload(value: Mapping[str, Any]) -> bool:
        return {"direction", "confidence", "regime"}.issubset(value.keys())


__all__ = [
    "CodexSource",
    "CodexSourceError",
    "InvalidCodexLookbackError",
    "InvalidCodexTimeoutError",
]
