from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from typing import Any

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from web.sse import subscribe

STATIC_DIR = Path(__file__).parent / "static"

APP_VERSION = "0.1.0"
PHASE_STATUS = "in_progress"
_STARTED_AT = time.monotonic()


@dataclass(frozen=True, slots=True)
class PhaseStatus:
    id: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class HealthStatus:
    version: str
    phase_status: PhaseStatus
    uptime_seconds: float


def create_app() -> FastAPI:
    application = FastAPI(title="Kraken Bot V4 Dashboard")
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_api_route("/api/health", healthcheck, methods=["GET"])
    application.add_api_route("/sse/updates", stream_updates, methods=["GET"])
    application.add_api_route("/", serve_index, methods=["GET"])
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return application


async def serve_index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def healthcheck() -> dict[str, Any]:
    return asdict(_health_status())


async def stream_updates() -> StreamingResponse:
    async def event_stream() -> AsyncIterator[bytes]:
        async with subscribe() as events:
            try:
                async for event in events:
                    yield event.encode()
            except asyncio.CancelledError:
                raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _health_status() -> HealthStatus:
    return HealthStatus(
        version=APP_VERSION,
        phase_status=PhaseStatus(id="5", name="Observability", status=PHASE_STATUS),
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 3),
    )


app = create_app()


__all__ = ["APP_VERSION", "PHASE_STATUS", "app", "create_app", "healthcheck", "stream_updates"]
