from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SSEMessage:
    event: str
    data: Any
    event_id: str | None = None

    def encode(self) -> bytes:
        payload = json.dumps(self.data, separators=(",", ":"), sort_keys=True)
        lines = []
        if self.event_id is not None:
            lines.append(f"id: {self.event_id}")
        lines.append(f"event: {self.event}")
        lines.append(f"data: {payload}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")


_subscribers: set[asyncio.Queue[SSEMessage]] = set()


async def publish(*, event: str, data: Any, event_id: str | None = None) -> None:
    message = SSEMessage(event=event, data=data, event_id=event_id)
    for subscriber in tuple(_subscribers):
        subscriber.put_nowait(message)


@asynccontextmanager
async def subscribe() -> AsyncIterator[AsyncIterator[SSEMessage]]:
    queue: asyncio.Queue[SSEMessage] = asyncio.Queue()
    _subscribers.add(queue)
    try:
        yield _iterate(queue)
    finally:
        _subscribers.discard(queue)


async def _iterate(queue: asyncio.Queue[SSEMessage]) -> AsyncIterator[SSEMessage]:
    while True:
        yield await queue.get()


__all__ = ["SSEMessage", "publish", "subscribe"]
