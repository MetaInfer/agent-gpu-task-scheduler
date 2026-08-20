"""Worker daemon: outbound WSS connection, heartbeats, and idempotent commands."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from agent_scheduler.adapters.harness import HarnessAdapter
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
from agent_scheduler.integrity import canonical_bytes
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
    controller: HarnessAdapter | None = None
    _send_sequence: int = 0
    _seen_sequence: int = 0
    _responses: dict[str, ProtocolEnvelope] = field(default_factory=dict)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _background: set[asyncio.Task[None]] = field(default_factory=set)
    _protocol_dir: Path = field(init=False)
    _protocol_lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self._protocol_dir = self.driver.events.root / "worker-inbox" / self.worker_id / "protocol"
        (self._protocol_dir / "responses").mkdir(parents=True, exist_ok=True)
        state_path = self._protocol_dir / "state.json"
        if state_path.is_file():
            value = json.loads(state_path.read_text(encoding="utf-8"))
            self._send_sequence = int(value.get("send_sequence", 0))
            self._seen_sequence = int(value.get("seen_sequence", 0))
        for path in sorted((self._protocol_dir / "responses").glob("*.json")):
            envelope = ProtocolEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
            original_id = path.stem
            self._responses[original_id] = envelope

    async def run(self) -> None:
        context = ssl.create_default_context(cafile=self.ca_file)
        headers = {
            "X-Worker-ID": self.worker_id,
            "Authorization": f"Bearer {self.api_key}",
        }
        delay = 1
        while True:
            try:
                await self._session(context, headers)
                delay = 1
            except (ConnectionClosed, OSError, ssl.SSLError):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def _session(self, context: ssl.SSLContext, headers: dict[str, str]) -> None:
        async with connect(
            self.uri,
            additional_headers=headers,
            ssl=context,
            ping_interval=10,
            ping_timeout=30,
            max_size=2 * 1024 * 1024,
        ) as websocket:
            for response in self._responses.values():
                await self._send(websocket, response)
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
                    self._persist_protocol_state()
                    if envelope.message_type == "ACK":
                        reply_to = envelope.payload.get("reply_to")
                        if isinstance(reply_to, str):
                            self._ack_response(reply_to)
                        continue
                    cached = self._responses.get(envelope.message_id)
                    if cached is not None:
                        await self._send(websocket, cached)
                        continue
                    if envelope.message_type == "EXECUTE":
                        background = asyncio.create_task(self._handle_and_send(websocket, envelope))
                        self._background.add(background)
                        background.add_done_callback(self._background.discard)
                        continue
                    response = await asyncio.to_thread(self._handle, envelope)
                    self._record_response(envelope.message_id, response)
                    await self._send(websocket, response)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                if self._background:
                    await asyncio.gather(*self._background, return_exceptions=True)

    async def _handle_and_send(
        self, websocket: ClientConnection, envelope: ProtocolEnvelope
    ) -> None:
        response = await asyncio.to_thread(self._handle, envelope)
        self._record_response(envelope.message_id, response)
        await self._send(websocket, response)

    def _record_response(self, original_id: str, response: ProtocolEnvelope) -> None:
        with self._protocol_lock:
            self._responses[original_id] = response
            self._atomic_write(
                self._protocol_dir / "responses" / f"{original_id}.json",
                response.model_dump_json() + "\n",
            )

    def _ack_response(self, response_id: str) -> None:
        with self._protocol_lock:
            original = next(
                (
                    original_id
                    for original_id, response in self._responses.items()
                    if response.message_id == response_id
                ),
                None,
            )
            if original is None:
                return
            self._responses.pop(original, None)
            path = self._protocol_dir / "responses" / f"{original}.json"
            if path.exists():
                path.unlink()

    def _persist_protocol_state(self) -> None:
        with self._protocol_lock:
            self._atomic_write(
                self._protocol_dir / "state.json",
                json.dumps(
                    {
                        "send_sequence": self._send_sequence,
                        "seen_sequence": self._seen_sequence,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n",
            )

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

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
            task = self._load_task(envelope)
            payload = {"accepted": self.driver.prepare(manifest, task)}
        elif envelope.message_type == "PLAN":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = self._load_task(envelope)
            payload = {"accepted": self.driver.acknowledge_plan(plan, task)}
        elif envelope.message_type == "EXECUTE":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = self._load_task(envelope)
            cached_result = self._completed_execution(plan, task)
            if cached_result is not None:
                payload = cached_result
            else:
                if self.controller is not None:
                    self.controller.control(plan.assignment_id, plan.dispatch_generation)
                result = self.driver.execute(plan, task)
                payload = {
                    "exit_code": result.exit_code,
                    "logs_present": result.logs_present,
                    "outputs_present": result.outputs_present,
                    "cleanup_ok": result.cleanup_ok,
                    "warning": result.warning,
                    "failure_reason": result.failure_reason,
                }
        elif envelope.message_type == "STATUS_QUERY":
            task = self._load_task(envelope)
            if (
                envelope.assignment_id is None
                or envelope.dispatch_generation is None
                or envelope.lease_epoch is None
            ):
                raise WorkerClientError("STATUS_QUERY lacks assignment fencing fields")
            payload = {
                "status": self.driver.query_status(
                    envelope.assignment_id,
                    envelope.dispatch_generation,
                    envelope.lease_epoch,
                    task,
                )
            }
        elif envelope.message_type == "CANCEL":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = self._load_task(envelope)
            payload = {"cancelled": self.driver.cancel(plan, task)}
        elif envelope.message_type == "RECONCILE":
            plan = strict_from_json_value(ExecutionPlan, envelope.payload["plan"])
            task = self._load_task(envelope)
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

    def _completed_execution(self, plan: ExecutionPlan, task: Task) -> dict[str, Any] | None:
        path = (
            self.driver.events.root
            / "worker-inbox"
            / self.worker_id
            / task.execution_id
            / "driver-events.jsonl"
        )
        if not path.is_file():
            return None
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            value = json.loads(line)
            if (
                isinstance(value, dict)
                and value.get("event_type") == "EXECUTION_FINISHED"
                and value.get("task_id") == task.task_id
                and isinstance(value.get("payload"), dict)
                and value["payload"].get("plan_id") == plan.plan_id
            ):
                evidence = value["payload"]
                return {
                    "exit_code": evidence.get("exit_code", 1),
                    "logs_present": evidence.get("logs_present") is True,
                    "outputs_present": evidence.get("outputs_present") is True,
                    "cleanup_ok": evidence.get("cleanup_ok") is True,
                    "warning": evidence.get("warning"),
                    "failure_reason": evidence.get("failure_reason"),
                }
        return None

    def _load_task(self, envelope: ProtocolEnvelope) -> Task:
        task_id = envelope.payload.get("task_id")
        expected_hash = envelope.payload.get("task_content_hash")
        task_path_value = envelope.payload.get("task_path")
        canonical_path_value = envelope.payload.get("task_canonical_path")
        if not isinstance(task_id, str) or not isinstance(expected_hash, str):
            raise WorkerClientError("Worker message lacks Task identity/hash")
        if not isinstance(task_path_value, str) or not isinstance(canonical_path_value, str):
            raise WorkerClientError("Worker message lacks Task Ground Truth paths")
        task_path = Path(task_path_value).resolve()
        canonical_path = Path(canonical_path_value).resolve()
        expected_task_path = (
            self.driver.events.root / "immutable" / "tasks" / f"{task_id}.json"
        ).resolve()
        expected_canonical_path = (
            self.driver.events.root / "immutable" / "tasks" / f"{task_id}.canonical.json"
        ).resolve()
        if task_path != expected_task_path or canonical_path != expected_canonical_path:
            raise WorkerClientError("Task reference escapes the configured Ground Truth")
        try:
            task = Task.model_validate_json(task_path.read_text(encoding="utf-8"))
            canonical = canonical_path.read_bytes()
        except (OSError, ValueError) as exc:
            raise WorkerClientError("Task Ground Truth is missing or invalid") from exc
        if task.task_id != task_id or task.content_hash != expected_hash:
            raise WorkerClientError("Task Ground Truth differs from the signed message reference")
        if canonical != canonical_bytes(task):
            raise WorkerClientError("canonical Task sidecar differs from Task JSON")
        return task

    def _envelope(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        assignment_id: str | None = None,
        dispatch_generation: int | None = None,
        lease_epoch: int | None = None,
    ) -> ProtocolEnvelope:
        with self._protocol_lock:
            self._send_sequence += 1
            self._persist_protocol_state()
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
