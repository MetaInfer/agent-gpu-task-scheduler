"""Reliable WSS request/response transport between Master and Worker."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from agent_scheduler.domain.models import (
    ExecutionPlan,
    GpuState,
    PrepareManifest,
    ProtocolEnvelope,
    Task,
    WorkerSnapshot,
    new_id,
    strict_from_json_value,
    utc_now,
)
from agent_scheduler.scheduler.core import ExecutionResult, WorkerDriver
from agent_scheduler.storage.events import EventStore


class WorkerProtocolError(RuntimeError):
    pass


class WorkerHub:
    def __init__(self, events: EventStore, on_heartbeat: Callable[[WorkerSnapshot], None]) -> None:
        self.events = events
        self.on_heartbeat = on_heartbeat
        self._connections: dict[str, WebSocket] = {}
        self._pending: dict[str, asyncio.Future[ProtocolEnvelope]] = {}
        self._sequence: dict[str, int] = {}
        self._receive_sequence: dict[str, int] = {}
        self._last_snapshot: dict[str, WorkerSnapshot] = {}
        for value in events.iter_snapshots("protocol-sequence"):
            worker = value.get("worker_id")
            sequence = value.get("sequence")
            if isinstance(worker, str) and isinstance(sequence, int):
                self._sequence[worker] = sequence
        for value in events.iter_snapshots("worker-receive-sequence"):
            worker = value.get("worker_id")
            sequence = value.get("sequence")
            if isinstance(worker, str) and isinstance(sequence, int):
                self._receive_sequence[worker] = sequence
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = asyncio.Lock()

    async def serve(self, worker_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            if worker_id in self._connections:
                await websocket.close(code=4409, reason="worker already connected")
                return
            self._connections[worker_id] = websocket
            self._loop = asyncio.get_running_loop()
        try:
            while True:
                envelope = ProtocolEnvelope.model_validate_json(await websocket.receive_text())
                prior_sequence = self._receive_sequence.get(worker_id, 0)
                if envelope.sequence <= prior_sequence:
                    if self.events.immutable_exists("worker-replies", envelope.message_id):
                        await self._send_ack(worker_id, envelope)
                    continue
                if envelope.sequence != prior_sequence + 1:
                    raise WorkerProtocolError(
                        f"Worker sequence gap: expected {prior_sequence + 1}, "
                        f"got {envelope.sequence}"
                    )
                self._receive_sequence[worker_id] = envelope.sequence
                self.events.write_snapshot(
                    "worker-receive-sequence",
                    _worker_object_id(worker_id),
                    {"worker_id": worker_id, "sequence": envelope.sequence},
                )
                self.events.append(
                    "workers",
                    _worker_object_id(worker_id),
                    f"MESSAGE_{envelope.message_type}",
                    worker_id,
                    envelope.message_id,
                    {
                        "sequence": envelope.sequence,
                        "assignment_id": envelope.assignment_id,
                        "dispatch_generation": envelope.dispatch_generation,
                        "lease_epoch": envelope.lease_epoch,
                    },
                )
                if envelope.message_type == "HEARTBEAT":
                    snapshot = strict_from_json_value(WorkerSnapshot, envelope.payload["worker"])
                    if snapshot.worker_id != worker_id:
                        raise WorkerProtocolError("heartbeat worker_id mismatch")
                    self._last_snapshot[worker_id] = snapshot
                    asyncio.create_task(asyncio.to_thread(self.on_heartbeat, snapshot))
                    await self._send_ack(worker_id, envelope)
                    continue
                reply_to = envelope.payload.get("reply_to")
                if isinstance(reply_to, str):
                    future = self._pending.get(reply_to)
                    if future is not None and not future.done():
                        if not self.events.immutable_exists("worker-replies", envelope.message_id):
                            self.events.write_immutable(
                                "worker-replies", envelope.message_id, envelope
                            )
                        future.set_result(envelope)
                        await self._send_ack(worker_id, envelope)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._lock:
                self._connections.pop(worker_id, None)
            prior = self._last_snapshot.get(worker_id)
            if prior is not None:
                offline = prior.model_copy(
                    update={
                        "online": False,
                        "last_heartbeat_at": utc_now(),
                        "gpus": tuple(
                            gpu.model_copy(update={"state": GpuState.UNKNOWN}) for gpu in prior.gpus
                        ),
                    }
                )
                await asyncio.to_thread(self.on_heartbeat, offline)

    def request(
        self,
        worker_id: str,
        message_type: str,
        payload: dict[str, Any],
        *,
        assignment_id: str,
        dispatch_generation: int,
        lease_epoch: int,
        timeout: float,
    ) -> ProtocolEnvelope:
        if self._loop is None:
            raise WorkerProtocolError("WorkerHub has no running event loop")
        future = asyncio.run_coroutine_threadsafe(
            self._request(
                worker_id,
                message_type,
                payload,
                assignment_id=assignment_id,
                dispatch_generation=dispatch_generation,
                lease_epoch=lease_epoch,
                timeout=timeout,
            ),
            self._loop,
        )
        return future.result(timeout=timeout + 5)

    async def _request(
        self,
        worker_id: str,
        message_type: str,
        payload: dict[str, Any],
        *,
        assignment_id: str,
        dispatch_generation: int,
        lease_epoch: int,
        timeout: float,
    ) -> ProtocolEnvelope:
        websocket = self._connections.get(worker_id)
        if websocket is None:
            raise WorkerProtocolError("Worker is not connected")
        sequence = self._sequence.get(worker_id, 0) + 1
        self._sequence[worker_id] = sequence
        self.events.write_snapshot(
            "protocol-sequence",
            _worker_object_id(worker_id),
            {"worker_id": worker_id, "sequence": sequence},
        )
        envelope = ProtocolEnvelope(
            message_id=new_id("msg"),
            sequence=sequence,
            message_type=message_type,
            assignment_id=assignment_id,
            dispatch_generation=dispatch_generation,
            lease_epoch=lease_epoch,
            payload=payload,
        )
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[ProtocolEnvelope] = loop.create_future()
        self._pending[envelope.message_id] = waiter
        self.events.append(
            "workers",
            _worker_object_id(worker_id),
            f"SEND_{message_type}",
            "master",
            envelope.message_id,
            {"sequence": sequence, "assignment_id": assignment_id},
        )
        try:
            await websocket.send_text(envelope.model_dump_json())
            return await asyncio.wait_for(waiter, timeout=timeout)
        finally:
            self._pending.pop(envelope.message_id, None)

    async def _send_ack(self, worker_id: str, received: ProtocolEnvelope) -> None:
        websocket = self._connections[worker_id]
        sequence = self._sequence.get(worker_id, 0) + 1
        self._sequence[worker_id] = sequence
        self.events.write_snapshot(
            "protocol-sequence",
            _worker_object_id(worker_id),
            {"worker_id": worker_id, "sequence": sequence},
        )
        ack = ProtocolEnvelope(
            message_id=new_id("msg"),
            sequence=sequence,
            message_type="ACK",
            assignment_id=received.assignment_id,
            dispatch_generation=received.dispatch_generation,
            lease_epoch=received.lease_epoch,
            payload={"reply_to": received.message_id},
        )
        await websocket.send_text(ack.model_dump_json())


class RemoteWorkerDriver(WorkerDriver):
    def __init__(self, hub: WorkerHub, state_root: str, worker_id: str | None = None) -> None:
        self.hub = hub
        self.state_root = state_root
        self.worker_id = worker_id
        self._assignment_workers: dict[str, str] = {}

    def prepare(self, manifest: PrepareManifest, task: Task) -> bool:
        # Record the route before the request: a timeout must still allow the Scheduler's
        # fencing/status query to reach the Worker that may have started preparing.
        self._assignment_workers[manifest.assignment_id] = manifest.worker_id
        response = self.hub.request(
            manifest.worker_id,
            "PREPARE",
            {"manifest": manifest.model_dump(mode="json"), **self._task_reference(task)},
            assignment_id=manifest.assignment_id,
            dispatch_generation=manifest.dispatch_generation,
            lease_epoch=manifest.lease_epoch,
            timeout=30,
        )
        self._ingest_worker_evidence(task, manifest.worker_id)
        return response.payload.get("accepted") is True

    def acknowledge_plan(self, plan: ExecutionPlan, task: Task) -> bool:
        response = self.hub.request(
            plan.worker_id,
            "PLAN",
            {**self._plan_reference(plan), **self._task_reference(task)},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=30,
        )
        self._ingest_worker_evidence(task, plan.worker_id)
        return response.payload.get("accepted") is True

    def execute(self, plan: ExecutionPlan, task: Task) -> ExecutionResult:
        response = self.hub.request(
            plan.worker_id,
            "EXECUTE",
            {**self._plan_reference(plan), **self._task_reference(task)},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=max(unit.timeout_seconds for unit in task.units) + 180,
        )
        self._ingest_worker_evidence(task, plan.worker_id)
        return ExecutionResult(
            exit_code=int(response.payload["exit_code"]),
            logs_present=response.payload.get("logs_present") is True,
            outputs_present=response.payload.get("outputs_present") is True,
            cleanup_ok=response.payload.get("cleanup_ok") is True,
            warning=_optional_string(response.payload.get("warning")),
            failure_reason=_optional_string(response.payload.get("failure_reason")),
        )

    def query_status(
        self,
        assignment_id: str,
        dispatch_generation: int,
        lease_epoch: int,
        task: Task,
    ) -> str:
        worker_id = self._assignment_workers.get(assignment_id, self.worker_id)
        if worker_id is None:
            raise WorkerProtocolError("assignment has no known Worker")
        response = self.hub.request(
            worker_id,
            "STATUS_QUERY",
            self._task_reference(task),
            assignment_id=assignment_id,
            dispatch_generation=dispatch_generation,
            lease_epoch=lease_epoch,
            timeout=30,
        )
        value = response.payload.get("status")
        return value if isinstance(value, str) else "UNKNOWN"

    def cancel(self, plan: ExecutionPlan, task: Task) -> bool:
        response = self.hub.request(
            plan.worker_id,
            "CANCEL",
            {**self._plan_reference(plan), **self._task_reference(task)},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=60,
        )
        self._ingest_worker_evidence(task, plan.worker_id)
        return response.payload.get("cancelled") is True

    def reconcile(self, plan: ExecutionPlan, task: Task) -> bool:
        response = self.hub.request(
            plan.worker_id,
            "RECONCILE",
            {**self._plan_reference(plan), **self._task_reference(task)},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=30,
        )
        self._ingest_worker_evidence(task, plan.worker_id)
        return response.payload.get("safe") is True

    def _ingest_worker_evidence(self, task: Task, worker_id: str) -> None:
        path = (
            Path(self.state_root)
            / "worker-inbox"
            / worker_id
            / task.execution_id
            / "driver-events.jsonl"
        )
        if not path.is_file():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict) or value.get("task_id") != task.task_id:
                raise WorkerProtocolError("Worker evidence is malformed or cross-bound")
            event_id = value.get("event_id")
            if not isinstance(event_id, str):
                raise WorkerProtocolError("Worker evidence lacks event_id")
            if not self.hub.events.immutable_exists("worker-evidence", event_id):
                self.hub.events.write_immutable("worker-evidence", event_id, value)

    def _plan_reference(self, plan: ExecutionPlan) -> dict[str, object]:
        if plan.content_hash is None:
            raise WorkerProtocolError("ExecutionPlan has no content hash")
        return {
            "plan_id": plan.plan_id,
            "plan_content_hash": plan.content_hash,
            "plan_path": f"{self.state_root}/immutable/plans/{plan.plan_id}.json",
            "plan_canonical_path": (
                f"{self.state_root}/immutable/plans/{plan.plan_id}.canonical.json"
            ),
        }

    def _task_reference(self, task: Task) -> dict[str, object]:
        if task.content_hash is None:
            raise WorkerProtocolError("Task has no content hash")
        return {
            "task_id": task.task_id,
            "task_content_hash": task.content_hash,
            "task_path": f"{self.state_root}/immutable/tasks/{task.task_id}.json",
            "task_canonical_path": (
                f"{self.state_root}/immutable/tasks/{task.task_id}.canonical.json"
            ),
        }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _worker_object_id(worker_id: str) -> str:
    import hashlib

    return f"worker_{hashlib.sha256(worker_id.encode()).hexdigest()[:32]}"
