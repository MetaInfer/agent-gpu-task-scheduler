import json
from pathlib import Path

import pytest

from agent_scheduler.domain.models import new_id
from agent_scheduler.storage.events import EventStore, StoreCorruptionError


def test_events_are_monotonic_and_replayable(tmp_path: Path):
    store = EventStore(tmp_path / "state")
    object_id = new_id("task")
    first = store.append("tasks", object_id, "CREATED", "test", "req-1")
    second = store.append("tasks", object_id, "QUEUED", "test", "req-2", {"n": 1})
    assert (first.sequence, second.sequence) == (1, 2)
    assert [event.event_type for event in store.list_events("tasks", object_id)] == [
        "CREATED",
        "QUEUED",
    ]
    assert [event.sequence for event in store.list_events("tasks", object_id, 1)] == [2]


def test_immutable_object_cannot_be_overwritten(tmp_path: Path):
    store = EventStore(tmp_path / "state")
    object_id = new_id("task")
    store.write_immutable("tasks", object_id, {"value": 1})
    with pytest.raises(FileExistsError):
        store.write_immutable("tasks", object_id, {"value": 2})


def test_partial_event_tail_is_rejected(tmp_path: Path):
    store = EventStore(tmp_path / "state")
    object_id = new_id("task")
    store.append("tasks", object_id, "CREATED", "test", "req")
    path = store.events_root / "tasks" / f"{object_id}.jsonl"
    with path.open("ab") as handle:
        handle.write(b'{"partial":true}')
    with pytest.raises(StoreCorruptionError, match="partial event line"):
        store.list_events("tasks", object_id)


def test_sequence_gap_is_rejected(tmp_path: Path):
    store = EventStore(tmp_path / "state")
    object_id = new_id("task")
    event = store.append("tasks", object_id, "CREATED", "test", "req")
    data = event.model_dump(mode="json")
    data["sequence"] = 3
    path = store.events_root / "tasks" / f"{object_id}.jsonl"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    with pytest.raises(StoreCorruptionError, match="sequence gap"):
        store.list_events("tasks", object_id)
