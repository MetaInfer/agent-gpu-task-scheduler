"""Append-only events and immutable objects for local/NFS Ground Truth."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agent_scheduler.domain.models import ID, Event, new_id, utc_now


class StoreCorruptionError(RuntimeError):
    pass


class EventStore:
    """Single-process writer with fsync and atomic snapshot replacement."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.immutable_root = root / "immutable"
        self.events_root = root / "events"
        self.snapshots_root = root / "snapshots"
        self._lock = threading.RLock()
        for path in (self.root, self.immutable_root, self.events_root, self.snapshots_root):
            path.mkdir(parents=True, exist_ok=True)

    def write_immutable(
        self, object_type: str, object_id: str, payload: BaseModel | Mapping[str, Any]
    ) -> Path:
        path = self.immutable_root / object_type / f"{object_id}.json"
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        with self._lock:
            if path.exists():
                raise FileExistsError(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(path, data)
        return path

    def write_immutable_bytes(self, object_type: str, name: str, payload: bytes) -> Path:
        path = self.immutable_root / object_type / name
        with self._lock:
            if path.exists():
                raise FileExistsError(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_bytes(path, payload)
        return path

    def immutable_exists(self, object_type: str, object_id: str) -> bool:
        return (self.immutable_root / object_type / f"{object_id}.json").is_file()

    def read_immutable(self, object_type: str, object_id: str) -> dict[str, Any]:
        return self._read_object(self.immutable_root / object_type / f"{object_id}.json")

    def iter_immutable(self, object_type: str) -> Iterator[dict[str, Any]]:
        directory = self.immutable_root / object_type
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            if path.name.endswith(".canonical.json"):
                continue
            yield self._read_object(path)

    def write_snapshot(
        self, object_type: str, object_id: str, payload: BaseModel | Mapping[str, Any]
    ) -> Path:
        path = self.snapshots_root / object_type / f"{object_id}.json"
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else dict(payload)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(path, data)
        return path

    def read_snapshot(self, object_type: str, object_id: str) -> dict[str, Any] | None:
        path = self.snapshots_root / object_type / f"{object_id}.json"
        return self._read_object(path) if path.exists() else None

    def delete_snapshot(self, object_type: str, object_id: str) -> None:
        path = self.snapshots_root / object_type / f"{object_id}.json"
        with self._lock:
            if path.exists():
                path.unlink()
                self._fsync_directory(path.parent)

    def iter_snapshots(self, object_type: str) -> Iterator[dict[str, Any]]:
        directory = self.snapshots_root / object_type
        if not directory.exists():
            return
        for path in sorted(directory.glob("*.json")):
            yield self._read_object(path)

    def append(
        self,
        object_type: str,
        object_id: ID,
        event_type: str,
        actor: str,
        request_id: str,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """Append and fsync exactly one event before returning."""
        with self._lock:
            path = self.events_root / object_type / f"{object_id}.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            sequence = self._next_sequence(path)
            event = Event(
                event_id=new_id("evt"),
                object_type=object_type,
                object_id=object_id,
                sequence=sequence,
                event_type=event_type,
                actor=actor,
                request_id=request_id,
                timestamp=utc_now(),
                payload=payload or {},
            )
            line = (
                json.dumps(
                    event.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n"
            )
            created = not path.exists()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
            try:
                written = os.write(fd, line)
                if written != len(line):
                    raise OSError("partial event append")
                os.fsync(fd)
            finally:
                os.close(fd)
            if created:
                self._fsync_directory(path.parent)
            return event

    def list_events(self, object_type: str, object_id: str, after_sequence: int = 0) -> list[Event]:
        path = self.events_root / object_type / f"{object_id}.jsonl"
        if not path.exists():
            return []
        events: list[Event] = []
        expected = 1
        with path.open(encoding="utf-8", newline="") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise StoreCorruptionError(f"partial event line {path}:{line_number}")
                try:
                    event = Event.model_validate_json(line)
                except ValueError as exc:
                    raise StoreCorruptionError(f"invalid event line {path}:{line_number}") from exc
                if event.object_type != object_type or event.object_id != object_id:
                    raise StoreCorruptionError(
                        f"event stream binding mismatch at {path}:{line_number}"
                    )
                if event.sequence != expected:
                    raise StoreCorruptionError(
                        f"event sequence gap at {path}:{line_number}: expected {expected}, got {event.sequence}"
                    )
                expected += 1
                if event.sequence > after_sequence:
                    events.append(event)
        return events

    def validate_all_events(self) -> None:
        for directory in sorted(self.events_root.iterdir()):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.jsonl")):
                self.list_events(directory.name, path.stem)

    def _next_sequence(self, path: Path) -> int:
        if not path.exists():
            return 1
        events = self.list_events(path.parent.name, path.stem)
        return events[-1].sequence + 1 if events else 1

    @staticmethod
    def _read_object(path: Path) -> dict[str, Any]:
        try:
            with path.open(encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreCorruptionError(f"invalid JSON object: {path}") from exc
        if not isinstance(value, dict):
            raise StoreCorruptionError(f"JSON value is not an object: {path}")
        return value

    @classmethod
    def _atomic_write_json(cls, path: Path, payload: Mapping[str, Any]) -> None:
        data = (
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
        ).encode("utf-8")
        cls._atomic_write_bytes(path, data)

    @classmethod
    def _atomic_write_bytes(cls, path: Path, payload: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            cls._fsync_directory(path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
