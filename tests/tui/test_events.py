"""Unit tests for tui.events — SSE stream parsing."""
from __future__ import annotations

import asyncio
import io
import json
from typing import Any
from unittest.mock import patch

from tui.events import read_sse_stream


def _make_sse_bytes(*events: tuple[str, dict[str, Any]]) -> bytes:
    """Build raw SSE bytes from (event_name, data) pairs."""
    parts: list[str] = []
    for name, data in events:
        parts.append(f"event: {name}")
        parts.append(f"data: {json.dumps(data)}")
        parts.append("")  # blank line = end of event
    # Trailing newline ensures the last blank line is read as a full line
    return ("\n".join(parts) + "\n").encode("utf-8")


class _FakeResponse:
    """Mimics a urllib response with readline()."""

    def __init__(self, raw: bytes) -> None:
        self._stream = io.BytesIO(raw)

    def readline(self) -> bytes:
        return self._stream.readline()

    def close(self) -> None:
        self._stream.close()


async def _collect(url: str, raw: bytes) -> list[tuple[str, dict]]:
    fake = _FakeResponse(raw)
    with patch("tui.events.urlopen", return_value=fake):
        return [(name, data) async for name, data in read_sse_stream(url)]


def test_single_event() -> None:
    raw = _make_sse_bytes(("dashboard.update", {"portfolio": {"cash_usd": "100"}}))
    collected = asyncio.run(_collect("http://fake/sse", raw))
    assert len(collected) == 1
    assert collected[0][0] == "dashboard.update"
    assert collected[0][1]["portfolio"]["cash_usd"] == "100"


def test_multiple_events() -> None:
    raw = _make_sse_bytes(
        ("dashboard.update", {"a": 1}),
        ("dashboard.update", {"b": 2}),
    )
    collected = asyncio.run(_collect("http://fake/sse", raw))
    assert len(collected) == 2
    assert collected[0][1] == {"a": 1}
    assert collected[1][1] == {"b": 2}


def test_ignores_id_and_comment_lines() -> None:
    raw = (
        b"id: 42\n"
        b": this is a comment\n"
        b"event: test\n"
        b"data: {\"ok\": true}\n"
        b"\n"
    )
    collected = asyncio.run(_collect("http://fake/sse", raw))
    assert len(collected) == 1
    assert collected[0][0] == "test"
    assert collected[0][1]["ok"] is True


def test_empty_stream() -> None:
    collected = asyncio.run(_collect("http://fake/sse", b""))
    assert collected == []


def test_malformed_json_yields_raw() -> None:
    raw = b"event: bad\ndata: not-json\n\n"
    collected = asyncio.run(_collect("http://fake/sse", raw))
    assert len(collected) == 1
    assert collected[0][1] == {"raw": "not-json"}
