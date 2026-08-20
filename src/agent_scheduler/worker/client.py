"""Worker daemon: outbound WSS connection, heartbeats, and idempotent commands."""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

from agent_scheduler.domain.models import (
    ExecutionPlan,
    PrepareManifest,
    ProtocolEnvelope,
    Task,
    WorkerSnapshot,
    new_id,
    strict_from_json_value,
    utc_now,
)
from agent_scheduler.worker.driver import DockerWorkerDriver
from agent_scheduler.worker.gpu import HySmiSampler


class WorkerClientError(RuntimeError):
    pass


@dataclass
class WorkerClient:
    uri: str
    worker_id: str
    api_key: str
    driver: DockerWorkerDriver
    sampler: HySmiSampler
    ca_file: str
    _send_sequence: int = 0
    _seen_sequence: int = 0
    _responses: dict[str, ProtocolEnvelope] = field(default_factory=dict)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run(self) -> None:
        context = ssl.create_default_context(cafile=self.ca_file)
        headers = {
            "X-Worker-ID": self.worker_id,
            "Authorization": f"Bearer {self.api_key}",
        }
        async with connect(
            self.uri,
            additional_headers=headers,
            ssl=context,
            ping_interval=10,
            ping_timeout=30,
            max_size=2 * 1024 * 1024,
        ) as websocket:
            heartbeat = asyncio.create_task(self._heartbeats(websocket))
            try:
                async for raw in websocket:
                    if not isinstance(raw, str):
                        raise WorkerClientError("binary Worker protocol message is forbidden")
                    envelope = ProtocolEnvelope.model_validate_json(raw)
                    if envelope.sequence <= self._seen_sequence:
                        cached = self._responses.get(envelope.message_id)
                        if cached is not None:
                            await self._send(websocket, cached)
                        continue
                    self._seen_sequence = envelope.sequence
                    if envelope.message_type == "ACK":
                        continue
                    cached = self._responses.get(envelope.message_id)
                    if cached is not None:
                        await self._send(websocket, cached)
                        continue
                    response = await asyncio.to_thread(self._handle, envelope)
                    self._responses[envelope.message_id] = response
                    await self._send(websocket, response)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)

    async def _heartbeats(self, websocket: ClientConnection) -> None:
        while True:
            snapshots = await asyncio.to_thread(self.sampler.sample)
            worker = WorkerSnapshot(
                worker_id=self.worker_id,
                online=True,
                last_heartbeat_at=utc_now(),
                gpus=snapshots,
            )
            heartbeat = self._envelope("HEARTBEAT", {"worker": worker.model_dump(mode="json")})
            await self._send(websocket, heartbeat)
            await asyncio.sleep(10)

    def _handle(self, envelope: ProtocolEnvelope) -> ProtocolEnvelope:
        payload: dict[str, Any]
        if envelope.message_type == "PREPARE":
            manifest = strict_from_json_value(PrepareManifest, envelope.payload["manifest"])
            task = strict_from_json_value(Task, envelope.payload["task"])
            payload = {"accepted": self.driver.prepare(manifest, task)}
        elif envelope.message_type == "PLAN":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = strict_from_json_value(Task, envelope.payload["task"])
            payload = {"accepted": self.driver.acknowledge_plan(plan, task)}
        elif envelope.message_type == "EXECUTE":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = strict_from_json_value(Task, envelope.payload["task"])
            result = self.driver.execute(plan, task)
            payload = {
                "exit_code": result.exit_code,
                "logs_present": result.logs_present,
                "outputs_present": result.outputs_present,
                "cleanup_ok": result.cleanup_ok,
                "warning": result.warning,
                "failure_reason": result.failure_reason,
            }
        elif envelope.message_type == "RECONCILE":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = strict_from_json_value(Task, envelope.payload["task"])
            payload = {"safe": self.driver.reconcile(plan, task)}
        else:
            payload = {"accepted": False, "error": "UNKNOWN_MESSAGE_TYPE"}
        payload["reply_to"] = envelope.message_id
        return self._envelope(
            f"{envelope.message_type}_RESULT",
            payload,
            assignment_id=envelope.assignment_id,
            dispatch_generation=envelope.dispatch_generation,
            lease_epoch=envelope.lease_epoch,
        )

    def _envelope(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        assignment_id: str | None = None,
        dispatch_generation: int | None = None,
        lease_epoch: int | None = None,
    ) -> ProtocolEnvelope:
        self._send_sequence += 1
        return ProtocolEnvelope(
            message_id=new_id("msg"),
            sequence=self._send_sequence,
            message_type=message_type,
            assignment_id=assignment_id,
            dispatch_generation=dispatch_generation,
            lease_epoch=lease_epoch,
            payload=payload,
        )

    async def _send(self, websocket: ClientConnection, envelope: ProtocolEnvelope) -> None:
        async with self._send_lock:
            await websocket.send(envelope.model_dump_json())
