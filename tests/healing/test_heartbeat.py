from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from healing.heartbeat import HeartbeatSnapshot, HeartbeatStatus, read_heartbeat, write_heartbeat


def _snapshot(*, timestamp: datetime | None = None) -> HeartbeatSnapshot:
    return HeartbeatSnapshot(
        timestamp=timestamp or datetime(2026, 3, 24, 20, 0, tzinfo=timezone.utc),
        bot_status=HeartbeatStatus.HEALTHY,
        active_positions_count=2,
        open_orders_count=5,
        last_reconciliation_age_sec=12.5,
        last_belief_age_sec=45.0,
        websocket_connected=True,
        supabase_connected=True,
    )


def test_write_and_read_heartbeat_round_trip(tmp_path) -> None:
    path = tmp_path / "state" / "bot-heartbeat.json"
    snapshot = _snapshot()

    write_heartbeat(snapshot, path)
    loaded = read_heartbeat(path, now=snapshot.timestamp)

    assert loaded == snapshot
    assert path.exists()


def test_read_heartbeat_returns_none_when_file_is_stale(tmp_path) -> None:
    path = tmp_path / "state" / "bot-heartbeat.json"
    snapshot = _snapshot()
    write_heartbeat(snapshot, path)

    loaded = read_heartbeat(path, now=snapshot.timestamp + timedelta(seconds=121))

    assert loaded is None


def test_read_heartbeat_uses_configurable_staleness_window(tmp_path) -> None:
    path = tmp_path / "state" / "bot-heartbeat.json"
    snapshot = _snapshot()
    write_heartbeat(snapshot, path)

    loaded = read_heartbeat(
        path,
        now=snapshot.timestamp + timedelta(seconds=30),
        stale_after_seconds=10,
    )

    assert loaded is None


def test_read_heartbeat_returns_none_when_file_is_missing(tmp_path) -> None:
    missing_path = tmp_path / "state" / "bot-heartbeat.json"

    assert read_heartbeat(missing_path) is None


def test_read_heartbeat_returns_none_for_corrupted_payloads(tmp_path) -> None:
    path = tmp_path / "state" / "bot-heartbeat.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    assert read_heartbeat(path) is None

    path.write_text(json.dumps({"timestamp": "2026-03-24T20:00:00Z"}), encoding="utf-8")

    assert read_heartbeat(path) is None
