"""Reliable WSS request/response transport between Master and Worker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from agent_scheduler.domain.models import (
    ExecutionPlan,
    PrepareManifest,
    ProtocolEnvelope,
    Task,
    WorkerSnapshot,
    new_id,
    strict_from_json_value,
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
                self.events.append(
                    "workers",
                    _worker_object_id(worker_id),
                    f"MESSAGE_{envelope.message_type}",
                    worker_id,
                    envelope.message_id,
                    {"sequence": envelope.sequence},
                )
                if envelope.message_type == "HEARTBEAT":
                    snapshot = strict_from_json_value(WorkerSnapshot, envelope.payload["worker"])
                    if snapshot.worker_id != worker_id:
                        raise WorkerProtocolError("heartbeat worker_id mismatch")
                    self.on_heartbeat(snapshot)
                    await self._send_ack(worker_id, envelope)
                    continue
                reply_to = envelope.payload.get("reply_to")
                if isinstance(reply_to, str):
                    future = self._pending.get(reply_to)
                    if future is not None and not future.done():
                        future.set_result(envelope)
        except WebSocketDisconnect:
            pass
        finally:
            async with self._lock:
                self._connections.pop(worker_id, None)

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
    def __init__(self, hub: WorkerHub, worker_id: str = "worker-local-01") -> None:
        self.hub = hub
        self.worker_id = worker_id

    def prepare(self, manifest: PrepareManifest, task: Task) -> bool:
        response = self.hub.request(
            self.worker_id,
            "PREPARE",
            {"manifest": manifest.model_dump(mode="json"), "task": task.model_dump(mode="json")},
            assignment_id=manifest.assignment_id,
            dispatch_generation=manifest.dispatch_generation,
            lease_epoch=manifest.lease_epoch,
            timeout=30,
        )
        return response.payload.get("accepted") is True

    def acknowledge_plan(self, plan: ExecutionPlan, task: Task) -> bool:
        response = self.hub.request(
            self.worker_id,
            "PLAN",
            {"plan": plan.model_dump(mode="json"), "task": task.model_dump(mode="json")},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=30,
        )
        return response.payload.get("accepted") is True

    def execute(self, plan: ExecutionPlan, task: Task) -> ExecutionResult:
        response = self.hub.request(
            self.worker_id,
            "EXECUTE",
            {"plan": plan.model_dump(mode="json"), "task": task.model_dump(mode="json")},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=max(unit.timeout_seconds for unit in task.units) + 180,
        )
        return ExecutionResult(
            exit_code=int(response.payload["exit_code"]),
            logs_present=response.payload.get("logs_present") is True,
            outputs_present=response.payload.get("outputs_present") is True,
            cleanup_ok=response.payload.get("cleanup_ok") is True,
            warning=_optional_string(response.payload.get("warning")),
            failure_reason=_optional_string(response.payload.get("failure_reason")),
        )

    def reconcile(self, plan: ExecutionPlan, task: Task) -> bool:
        response = self.hub.request(
            self.worker_id,
            "RECONCILE",
            {"plan": plan.model_dump(mode="json"), "task": task.model_dump(mode="json")},
            assignment_id=plan.assignment_id,
            dispatch_generation=plan.dispatch_generation,
            lease_epoch=plan.lease_epoch,
            timeout=30,
        )
        return response.payload.get("safe") is True


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _worker_object_id(worker_id: str) -> str:
    import hashlib

    return f"worker_{hashlib.sha256(worker_id.encode()).hexdigest()[:32]}"
