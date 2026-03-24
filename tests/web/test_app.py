from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from web.app import APP_VERSION, PHASE_STATUS, app, stream_updates
from web.sse import publish, subscribe


def test_health_endpoint_returns_version_phase_status_and_uptime() -> None:
    client = TestClient(app)

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "version": APP_VERSION,
        "phase_status": {
            "id": "5",
            "name": "Observability",
            "status": PHASE_STATUS,
        },
        "uptime_seconds": response.json()["uptime_seconds"],
    }
    assert response.json()["uptime_seconds"] >= 0


def test_publish_and_subscribe_round_trip() -> None:
    async def exercise() -> None:
        async with subscribe() as events:
            await publish(
                event="dashboard.update",
                data={"status": "ok"},
                event_id="evt-1",
            )
            event = await asyncio.wait_for(anext(events), timeout=1)
            assert event.event == "dashboard.update"
            assert event.data == {"status": "ok"}
            assert event.event_id == "evt-1"
            assert event.encode() == (
                b'id: evt-1\nevent: dashboard.update\ndata: {"status":"ok"}\n\n'
            )

    asyncio.run(exercise())


def test_stream_updates_emits_server_sent_event_chunks() -> None:
    async def exercise() -> None:
        response = await stream_updates()
        consumer = asyncio.create_task(
            asyncio.wait_for(anext(response.body_iterator), timeout=1)
        )

        await asyncio.sleep(0)
        await publish(
            event="dashboard.update",
            data={"cycle": 1},
            event_id="evt-2",
        )

        chunk = await consumer

        assert response.media_type == "text/event-stream"
        assert chunk == b'id: evt-2\nevent: dashboard.update\ndata: {"cycle":1}\n\n'

    asyncio.run(exercise())
