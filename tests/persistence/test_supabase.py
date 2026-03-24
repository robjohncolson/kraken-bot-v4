from __future__ import annotations

import json

from persistence.supabase import (
    OfflineSupabaseQueue,
    QueuedSupabaseMutation,
    SupabaseClient,
    SupabaseMutation,
    SupabaseOperation,
)


def test_enqueue_persists_queue_entries_as_jsonl(tmp_path) -> None:
    queue = OfflineSupabaseQueue(
        tmp_path / "queue.jsonl",
        id_source=iter(("entry-1",)).__next__,
        timestamp_source=lambda: "2026-03-24T12:00:00Z",
    )
    mutation = SupabaseMutation(
        table="positions",
        payload={"pair": "DOGE/USD", "tags": ["grid", "belief"]},
    )

    queued_mutation = queue.enqueue(mutation)

    assert queued_mutation.entry_id == "entry-1"
    assert queued_mutation.enqueued_at == "2026-03-24T12:00:00Z"

    raw_record = json.loads(queue.path.read_text(encoding="utf-8").splitlines()[0])
    assert raw_record["entry_id"] == "entry-1"
    assert raw_record["mutation"]["table"] == "positions"
    assert raw_record["mutation"]["operation"] == "upsert"
    assert raw_record["mutation"]["payload"] == {
        "pair": "DOGE/USD",
        "tags": ["grid", "belief"],
    }


def test_dequeue_returns_oldest_entries_and_rewrites_remaining_queue(tmp_path) -> None:
    queue = OfflineSupabaseQueue(
        tmp_path / "queue.jsonl",
        id_source=iter(("entry-1", "entry-2")).__next__,
        timestamp_source=lambda: "2026-03-24T12:00:00Z",
    )
    queue.enqueue(SupabaseMutation(table="positions", payload={"pair": "DOGE/USD"}))
    queue.enqueue(SupabaseMutation(table="beliefs", payload={"pair": "XRP/USD"}))

    dequeued = queue.dequeue(limit=1)
    remaining = queue.peek_all()

    assert [item.entry_id for item in dequeued] == ["entry-1"]
    assert [item.mutation.table for item in remaining] == ["beliefs"]
    assert len(queue.path.read_text(encoding="utf-8").splitlines()) == 1


def test_serialized_queue_entry_round_trips_into_replay_request() -> None:
    queued_mutation = QueuedSupabaseMutation(
        entry_id="entry-7",
        enqueued_at="2026-03-24T12:00:00Z",
        attempt_count=2,
        mutation=SupabaseMutation(
            table="ledger",
            operation=SupabaseOperation.UPSERT,
            payload={
                "pair": "DOGE/USD",
                "fills": [{"price": 0.125, "qty": 100}],
            },
            filters={"pair": "DOGE/USD"},
        ),
    )
    serialized = queued_mutation.to_record()
    round_tripped = QueuedSupabaseMutation.from_record(json.loads(json.dumps(serialized)))
    client = SupabaseClient(
        url="https://example.supabase.co",
        key="service-role-key",
    )

    replay_request = client.prepare_replay(round_tripped)

    assert "service-role-key" not in json.dumps(serialized)
    assert replay_request.endpoint == "https://example.supabase.co/rest/v1/ledger"
    assert replay_request.mutation.to_record() == serialized["mutation"]
