"""SSE stream reader with async iteration support.

Provides a thin async generator over a standard ``text/event-stream``
endpoint.  Reconnect / backoff logic lives in the caller (the app worker).
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.request import Request, urlopen

INITIAL_BACKOFF_SEC: float = 1.0
MAX_BACKOFF_SEC: float = 30.0
BACKOFF_MULTIPLIER: float = 2.0


async def read_sse_stream(
    url: str,
    *,
    timeout: int = 60,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Open *url* as an SSE connection and yield ``(event_name, data)`` pairs.

    Raises on connection failure.  Returns when the stream ends.
    """

    def _open():
        req = Request(url, headers={"Accept": "text/event-stream"})
        return urlopen(req, timeout=timeout)

    resp = await asyncio.to_thread(_open)

    current_event = "message"
    current_data: list[str] = []

    try:
        while True:
            raw = await asyncio.to_thread(resp.readline)
            if not raw:
                return  # stream ended
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

            if not line:
                # blank line → dispatch accumulated event
                if current_data:
                    joined = "\n".join(current_data)
                    try:
                        data: dict[str, Any] = json.loads(joined)
                    except json.JSONDecodeError:
                        data = {"raw": joined}
                    yield current_event, data
                current_event = "message"
                current_data = []
            elif line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                current_data.append(line.split(":", 1)[1].strip())
            # id:, retry:, and comment lines (starting with :) are ignored
    finally:
        resp.close()
