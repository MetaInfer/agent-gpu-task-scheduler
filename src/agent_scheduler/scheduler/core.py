"""Deterministic scheduler, signed barriers, and atomic resource ownership."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agent_scheduler.domain.models import (
    ExecutionPlan,
    GpuSnapshot,
    GpuState,
    PrepareManifest,
    ResourceLease,
    Task,
    TaskState,
    TaskStatus,
    WorkerSnapshot,
    new_id,
    strict_from_json_value,
    utc_now,
)
from agent_scheduler.domain.state import transition_task
from agent_scheduler.integrity.signing import canonical_bytes, sign_model, verify_model
from agent_scheduler.storage.events import EventStore


class SchedulingError(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    logs_present: bool
    outputs_present: bool
    cleanup_ok: bool
    warning: str | None = None
    failure_reason: str | None = None


class WorkerDriver(Protocol):
    def prepare(self, manifest: PrepareManifest, task: Task) -> bool: ...

    def acknowledge_plan(self, plan: ExecutionPlan, task: Task) -> bool: ...

    def execute(self, plan: ExecutionPlan, task: Task) -> ExecutionResult: ...

    def cancel(self, plan: ExecutionPlan, task: Task) -> bool: ...

    def query_status(
        self,
        assignment_id: str,
        dispatch_generation: int,
        lease_epoch: int,
        task: Task,
    ) -> str: ...

    def reconcile(self, plan: ExecutionPlan, task: Task) -> bool: ...


@dataclass
class _Queued:
    task: Task
    status: TaskStatus
    enqueued_at: datetime
    aging_started_at: datetime


class Scheduler:
    """Own queue order, barrier progression, signed plans, and resource leases."""

    def __init__(
        self,
        events: EventStore,
        signing_key: Ed25519PrivateKey,
        verification_key: Ed25519PublicKey,
        driver: WorkerDriver,
        *,
        key_id: str,
        vram_threshold: float = 2.0,
        authorized_gpu_ids: frozenset[int] = frozenset(range(8)),
        qualification_profile: bool = False,
    ) -> None:
        if vram_threshold > 2.0 and not qualification_profile:
            raise ValueError(
                "VRAM threshold above production default requires qualification profile"
            )
        self.events = events
        self.signing_key = signing_key
        self.verification_key = verification_key
        self.driver = driver
        self.key_id = key_id
        self.vram_threshold = vram_threshold
        self.authorized_gpu_ids = authorized_gpu_ids
        self.qualification_profile = qualification_profile
        self._workers: dict[str, WorkerSnapshot] = {}
        self._queue: dict[str, _Queued] = {}
        self._leases: dict[str, ResourceLease] = {}
        self._holds: set[tuple[str, str]] = set()
        self._epochs: dict[tuple[str, str], int] = {}
        self._tasks: dict[str, Task] = {}
        self._statuses: dict[str, TaskStatus] = {}
        self._initial_statuses: dict[str, TaskStatus] = {}
        self._plans: dict[str, ExecutionPlan] = {}
        self._cancel_requested: set[str] = set()
        self._lock = RLock()
        self._load()

    def register_worker(self, worker: WorkerSnapshot, request_id: str = "worker-register") -> None:
        with self._lock:
            self._workers[worker.worker_id] = worker
            self.events.write_immutable("worker-samples", new_id("sample"), worker)
            self.events.write_snapshot("workers", worker.worker_id, worker)
            self.events.append(
                "workers",
                _worker_object_id(worker.worker_id),
                "HEARTBEAT",
                worker.worker_id,
                request_id,
                {"online": worker.online},
            )

    def enqueue(self, task: Task, request_id: str = "scheduler") -> TaskStatus:
        with self._lock:
            if task.task_id in self._statuses:
                return self._initial_statuses[task.task_id]
            self._verify_task(task)
            self._persist_task_if_needed(task)
            now = utc_now()
            status = TaskStatus(
                task_id=task.task_id,
                execution_id=task.execution_id,
                state=TaskState.QUEUED,
                updated_at=now,
            )
            self._tasks[task.task_id] = task
            self._statuses[task.task_id] = status
            self._initial_statuses[task.task_id] = status
            self._queue[task.task_id] = _Queued(task, status, now, now)
            self._persist_status(status)
            self.events.append("tasks", task.task_id, "QUEUED", "scheduler", request_id, {})
            if self.qualification_profile:
                self.events.append(
                    "tasks",
                    task.task_id,
                    "QUALIFICATION_PROFILE_APPLIED",
                    "scheduler",
                    request_id,
                    {
                        "vram_threshold": self.vram_threshold,
                        "authorized_gpu_ids": sorted(self.authorized_gpu_ids),
                    },
                )
            return status

    def tick(self, request_id: str = "scheduler") -> list[TaskStatus]:
        """Claim one queued Task, then perform Worker I/O without the Scheduler lock."""
        claimed: tuple[Task, list[tuple[str, tuple[int, ...]]]] | None = None
        with self._lock:
            ordered = sorted(self._queue.values(), key=self._queue_key)
            for queued in ordered:
                selections = self._select_for_task(queued.task)
                if selections is None:
                    if (
                        self.qualification_profile
                        and (utc_now() - queued.enqueued_at).total_seconds() >= 30 * 60
                    ):
                        self._queue.pop(queued.task.task_id)
                        self._transition(
                            queued.task,
                            TaskState.BLOCKED,
                            request_id,
                            failure_reason="QUALIFICATION_GPU_WAIT_EXPIRED",
                        )
                    continue
                self._queue.pop(queued.task.task_id)
                claimed = (queued.task, selections)
                break
        if claimed is None:
            return []
        task, selections = claimed
        return [self._dispatch(task, selections, request_id)]

    def get_task(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise SchedulingError("task not found") from exc

    def get_status(self, task_id: str) -> TaskStatus:
        try:
            return self._statuses[task_id]
        except KeyError as exc:
            raise SchedulingError("task status not found") from exc

    def statuses(self) -> list[TaskStatus]:
        return sorted(self._statuses.values(), key=lambda status: status.task_id)

    def leases(self) -> list[ResourceLease]:
        return list(self._leases.values())

    def workers(self) -> list[WorkerSnapshot]:
        return list(self._workers.values())

    def plans(self) -> list[ExecutionPlan]:
        return list(self._plans.values())

    def queued_task_ids(self) -> list[str]:
        with self._lock:
            return [
                queued.task.task_id for queued in sorted(self._queue.values(), key=self._queue_key)
            ]

    def cancel_task(self, task_id: str, *, actor: str, request_id: str) -> TaskStatus:
        with self._lock:
            task = self.get_task(task_id)
            status = self.get_status(task_id)
            if status.state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CANCELLED,
                TaskState.CLEANUP_FAILED,
            }:
                return status
            if status.state in {TaskState.QUEUED, TaskState.BLOCKED}:
                self._queue.pop(task_id, None)
                return self._transition(task, TaskState.CANCELLED, request_id)
            self._cancel_requested.add(task_id)
            plans = [plan for plan in self._plans.values() if plan.task_id == task_id]
        if plans and not all(self.driver.cancel(plan, task) for plan in plans):
            with self._lock:
                self._cancel_requested.discard(task_id)
            self.events.append("tasks", task_id, "CANCEL_UNCONFIRMED", actor, request_id, {})
            raise SchedulingError("cancel could not confirm container stopped")
        self.events.append("tasks", task_id, "CANCEL_REQUESTED", actor, request_id, {})
        return self.get_status(task_id)

    def reconcile_execution(
        self, execution_id: str, *, actor: str, reason: str, request_id: str
    ) -> bool:
        """Release every lease for an execution or none of them."""
        if not reason.strip():
            raise SchedulingError("reconcile reason is required")
        with self._lock:
            leases = [
                lease for lease in self._leases.values() if lease.execution_id == execution_id
            ]
            task = next(
                (item for item in self._tasks.values() if item.execution_id == execution_id), None
            )
            if task is None:
                raise SchedulingError("execution not found")
            plans = [plan for plan in self._plans.values() if plan.execution_id == execution_id]
            if not leases:
                return True
            if len(plans) != len(task.units) or not all(
                self.driver.reconcile(plan, task) for plan in plans
            ):
                self.events.append(
                    "tasks",
                    task.task_id,
                    "RECONCILE_REFUSED",
                    actor,
                    request_id,
                    {"reason": reason},
                )
                return False
            for lease in leases:
                self._delete_lease(lease)
            status = self._statuses[task.task_id]
            if status.state is TaskState.CLEANUP_FAILED:
                status = status.model_copy(
                    update={
                        "state": status.underlying_outcome or TaskState.FAILED,
                        "updated_at": utc_now(),
                        "failure_reason": "ADMIN_RECONCILED",
                    }
                )
                self._statuses[task.task_id] = status
                self._persist_status(status)
            self.events.append(
                "tasks",
                task.task_id,
                "RECONCILED",
                actor,
                request_id,
                {"reason": reason, "released_lease_ids": [lease.lease_id for lease in leases]},
            )
            return True

    def _queue_key(self, queued: _Queued) -> tuple[int, datetime, str]:
        aged = (utc_now() - queued.aging_started_at).total_seconds() >= 3600
        return (0 if aged else 1, queued.enqueued_at, queued.task.task_id)

    def _select_for_task(self, task: Task) -> list[tuple[str, tuple[int, ...]]] | None:
        if len(task.units) > len(self._workers):
            return None
        selections: list[tuple[str, tuple[int, ...]]] = []
        used_workers: set[str] = set()
        for unit in task.units:
            if unit.worker_id in used_workers:
                return None
            if any(
                lease.worker_id == unit.worker_id and lease.container_name == unit.container_name
                for lease in self._leases.values()
            ):
                return None
            selection = self._select_gpus(unit.required_gpu_count, unit.worker_id)
            if selection is None:
                return None
            used_workers.add(selection[0])
            selections.append(selection)
        return selections

    def _select_gpus(self, count: int, required_worker: str) -> tuple[str, tuple[int, ...]] | None:
        worker = self._workers.get(required_worker)
        if worker is None or not worker.online or not self._heartbeat_fresh(worker):
            return None
        leased = {
            (lease.worker_id, gpu_id) for lease in self._leases.values() for gpu_id in lease.gpu_ids
        }
        available = [
            gpu.gpu_id
            for gpu in sorted(worker.gpus, key=lambda item: item.gpu_id)
            if gpu.gpu_id in self.authorized_gpu_ids
            and gpu.state is GpuState.AVAILABLE
            and gpu.vram_percent < self.vram_threshold
            and self._sample_fresh(gpu)
            and (worker.worker_id, gpu.gpu_id) not in leased
            and (worker.worker_id, f"gpu:{gpu.gpu_id}") not in self._holds
        ]
        if len(available) < count:
            return None
        return worker.worker_id, tuple(available[:count])

    def _dispatch(
        self,
        task: Task,
        selections: list[tuple[str, tuple[int, ...]]],
        request_id: str,
    ) -> TaskStatus:
        status = self._transition(task, TaskState.PREPARING, request_id)
        leases: list[ResourceLease] = []
        committed: list[ResourceLease] = []
        plans: list[ExecutionPlan] = []
        side_effect_possible = False
        worker_reply_uncertain = False
        prepare_rejected = False
        try:
            manifests: list[PrepareManifest] = []
            for unit, (worker_id, gpu_ids) in zip(task.units, selections, strict=True):
                resources = [f"gpu:{gpu_id}" for gpu_id in gpu_ids] + [
                    f"container:{unit.container_name}"
                ]
                if any((worker_id, resource) in self._holds for resource in resources):
                    raise SchedulingError("resource hold conflict")
                self._holds.update((worker_id, resource) for resource in resources)
                epoch = (
                    max(self._epochs.get((worker_id, resource), 0) for resource in resources) + 1
                )
                for resource in resources:
                    self._epochs[(worker_id, resource)] = epoch
                assignment_id = new_id("assign")
                manifest = sign_model(
                    PrepareManifest(
                        key_id=self.key_id,
                        manifest_id=new_id("manifest"),
                        task_id=task.task_id,
                        execution_id=task.execution_id,
                        assignment_id=assignment_id,
                        dispatch_generation=1,
                        worker_id=worker_id,
                        gpu_ids=gpu_ids,
                        lease_epoch=epoch,
                        container_name=unit.container_name,
                        created_at=utc_now(),
                    ),
                    self.signing_key,
                )
                self.events.write_immutable("manifests", manifest.manifest_id, manifest)
                self.events.write_immutable_bytes(
                    "manifests",
                    f"{manifest.manifest_id}.canonical.json",
                    canonical_bytes(manifest),
                )
                provisional = ResourceLease(
                    lease_id=new_id("lease"),
                    execution_id=task.execution_id,
                    worker_id=worker_id,
                    gpu_ids=gpu_ids,
                    container_name=unit.container_name,
                    lease_epoch=epoch,
                    committed=False,
                )
                leases.append(provisional)
                self._persist_lease(provisional)
                manifests.append(manifest)
                worker_reply_uncertain = True
                accepted = self.driver.prepare(manifest, task)
                worker_reply_uncertain = False
                if not accepted:
                    prepare_rejected = True
                    raise SchedulingError("Worker rejected PrepareManifest")
            self.events.append("tasks", task.task_id, "PREPARED", "scheduler", request_id, {})
            committed = [lease.model_copy(update={"committed": True}) for lease in leases]
            for lease in committed:
                self._persist_lease(lease)
            status = self._transition(task, TaskState.RESERVED, request_id)
            for unit, manifest in zip(task.units, manifests, strict=True):
                if task.content_hash is None:
                    raise SchedulingError("signed Task has no content hash")
                plan_id = new_id("plan")
                plan = sign_model(
                    ExecutionPlan(
                        key_id=self.key_id,
                        plan_id=plan_id,
                        assignment_id=manifest.assignment_id,
                        execution_id=task.execution_id,
                        task_id=task.task_id,
                        task_content_hash=task.content_hash,
                        dispatch_generation=manifest.dispatch_generation,
                        worker_id=manifest.worker_id,
                        unit_id=unit.unit_id,
                        gpu_ids=manifest.gpu_ids,
                        lease_epoch=manifest.lease_epoch,
                        container_name=unit.container_name,
                        submitter_username=unit.submitter_username,
                        container_user=unit.container_user,
                        image_digest=unit.image_digest,
                        environment=(
                            ("HIP_VISIBLE_DEVICES", ",".join(map(str, manifest.gpu_ids))),
                            ("ROCR_VISIBLE_DEVICES", ",".join(map(str, manifest.gpu_ids))),
                            ("TASK_ID", task.task_id),
                            ("UNIT_ID", unit.unit_id),
                            ("EXECUTION_ID", task.execution_id),
                            ("PLAN_ID", plan_id),
                            ("ASSIGNMENT_ID", manifest.assignment_id),
                            ("LEASE_EPOCH", str(manifest.lease_epoch)),
                        ),
                        created_at=utc_now(),
                    ),
                    self.signing_key,
                )
                self.events.write_immutable("plans", plan.plan_id, plan)
                self.events.write_immutable_bytes(
                    "plans", f"{plan.plan_id}.canonical.json", canonical_bytes(plan)
                )
                self._plans[plan.plan_id] = plan
                worker_reply_uncertain = True
                acknowledged = self.driver.acknowledge_plan(plan, task)
                worker_reply_uncertain = False
                if not acknowledged:
                    raise SchedulingError("Worker rejected ExecutionPlan")
                plans.append(plan)
            self.events.append("tasks", task.task_id, "PLAN_ACK", "scheduler", request_id, {})
            with self._lock:
                cancelled_before_start = task.task_id in self._cancel_requested
                self._cancel_requested.discard(task.task_id)
            if cancelled_before_start:
                status = self._transition(task, TaskState.FINALIZING, request_id)
                for lease in committed:
                    self._delete_lease(lease)
                return self._transition(
                    task,
                    TaskState.CANCELLED,
                    request_id,
                    failure_reason="CANCELLED_BEFORE_START",
                )
            status = self._transition(task, TaskState.DISPATCHED, request_id)
            status = self._transition(task, TaskState.STARTING, request_id)
            side_effect_possible = True
            status = self._transition(task, TaskState.RUNNING, request_id)
            worker_reply_uncertain = True
            results = [self.driver.execute(plan, task) for plan in plans]
            worker_reply_uncertain = False
            with self._lock:
                cancelled_during_run = task.task_id in self._cancel_requested
                self._cancel_requested.discard(task.task_id)
            outcome: TaskState
            failure_reason: str | None
            if cancelled_during_run:
                outcome, failure_reason = TaskState.CANCELLED, "CANCELLED_DURING_RUN"
            else:
                outcome, failure_reason = self._outcome(results)
            underlying = outcome
            status = self._transition(task, TaskState.FINALIZING, request_id)
            if any(not result.cleanup_ok for result in results):
                status = self._transition(
                    task,
                    TaskState.CLEANUP_FAILED,
                    request_id,
                    underlying_outcome=underlying,
                    failure_reason="CONTAINER_NOT_CONFIRMED_STOPPED",
                )
                return status
            for lease in committed:
                self._delete_lease(lease)
            return self._transition(task, outcome, request_id, failure_reason=failure_reason)
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            query_results: list[str] = []
            if worker_reply_uncertain:
                for manifest in manifests:
                    try:
                        query_results.append(
                            self.driver.query_status(
                                manifest.assignment_id,
                                manifest.dispatch_generation,
                                manifest.lease_epoch,
                                task,
                            )
                        )
                    except (OSError, RuntimeError, TimeoutError, ValueError):
                        query_results.append("UNKNOWN")
                self.events.append(
                    "tasks",
                    task.task_id,
                    "STATUS_QUERIED_AFTER_TIMEOUT",
                    "scheduler",
                    request_id,
                    {"results": query_results},
                )
            uncertain = worker_reply_uncertain or side_effect_possible or bool(committed)
            if (
                query_results
                and set(query_results) == {"NOT_REGISTERED"}
                and not committed
                and not side_effect_possible
            ):
                uncertain = False
            if prepare_rejected and not committed:
                uncertain = False
            target = TaskState.RECONCILIATION_REQUIRED if uncertain else TaskState.FAILED
            if not uncertain:
                for lease in leases:
                    self._delete_lease(lease)
            return self._force_failure(task, target, request_id, str(exc))
        finally:
            for worker_id, gpu_ids in selections:
                for gpu_id in gpu_ids:
                    self._holds.discard((worker_id, f"gpu:{gpu_id}"))
            for unit, (worker_id, _) in zip(task.units, selections, strict=True):
                self._holds.discard((worker_id, f"container:{unit.container_name}"))

    def _transition(
        self,
        task: Task,
        target: TaskState,
        request_id: str,
        *,
        underlying_outcome: TaskState | None = None,
        failure_reason: str | None = None,
    ) -> TaskStatus:
        current = self._statuses[task.task_id]
        transition_task(current.state, target)
        updated = current.model_copy(
            update={
                "state": target,
                "updated_at": utc_now(),
                "underlying_outcome": underlying_outcome,
                "failure_reason": failure_reason,
            }
        )
        self._statuses[task.task_id] = updated
        self._persist_status(updated)
        self.events.append(
            "tasks",
            task.task_id,
            target.value,
            "scheduler",
            request_id,
            {"failure_reason": failure_reason} if failure_reason else {},
        )
        return updated

    def _force_failure(
        self, task: Task, target: TaskState, request_id: str, reason: str
    ) -> TaskStatus:
        current = self._statuses[task.task_id]
        if current.state not in {
            TaskState.FINALIZING,
            TaskState.FAILED,
        } and TaskState.FINALIZING in _allowed_targets(current.state):
            current = self._transition(task, TaskState.FINALIZING, request_id)
        if current.state is TaskState.FINALIZING:
            return self._transition(
                task,
                target,
                request_id,
                underlying_outcome=TaskState.FAILED,
                failure_reason=reason,
            )
        updated = current.model_copy(
            update={"state": target, "updated_at": utc_now(), "failure_reason": reason}
        )
        self._statuses[task.task_id] = updated
        self._persist_status(updated)
        self.events.append(
            "tasks", task.task_id, target.value, "scheduler", request_id, {"error": reason}
        )
        return updated

    @staticmethod
    def _outcome(results: list[ExecutionResult]) -> tuple[TaskState, str | None]:
        if any(result.exit_code != 0 for result in results):
            return TaskState.FAILED, next(
                (result.failure_reason for result in results if result.exit_code != 0),
                "RUN_NONZERO",
            )
        if any(not result.logs_present for result in results):
            return TaskState.FAILED, "EXPECTED_LOG_MISSING"
        if any(not result.outputs_present for result in results):
            return TaskState.FAILED, "EXPECTED_OUTPUT_MISSING"
        return TaskState.COMPLETED, None

    def _verify_task(self, task: Task) -> None:
        if task.key_id != self.key_id or not verify_model(task, self.verification_key):
            raise SchedulingError("Task signature or key_id is invalid")

    def _persist_task_if_needed(self, task: Task) -> None:
        if not self.events.immutable_exists("tasks", task.task_id):
            self.events.write_immutable("tasks", task.task_id, task)
            return
        existing = strict_from_json_value(Task, self.events.read_immutable("tasks", task.task_id))
        if existing != task:
            raise SchedulingError("Task ID already exists with different content")

    def _persist_status(self, status: TaskStatus) -> None:
        self.events.write_immutable("task-status-history", new_id("status"), status)
        self.events.write_snapshot("task-status", status.task_id, status)

    def _persist_lease(self, lease: ResourceLease) -> None:
        self.events.write_immutable(
            "lease-history",
            new_id("lease_evt"),
            {
                "action": "COMMITTED" if lease.committed else "HELD",
                "timestamp": utc_now().isoformat(),
                "lease": lease.model_dump(mode="json"),
            },
        )
        self._leases[lease.lease_id] = lease
        self.events.write_snapshot("leases", lease.lease_id, lease)

    def _delete_lease(self, lease: ResourceLease) -> None:
        self.events.write_immutable(
            "lease-history",
            new_id("lease_evt"),
            {
                "action": "RELEASED",
                "timestamp": utc_now().isoformat(),
                "lease": lease.model_dump(mode="json"),
            },
        )
        self._leases.pop(lease.lease_id, None)
        self.events.delete_snapshot("leases", lease.lease_id)

    def _load(self) -> None:
        for data in self.events.iter_snapshots("workers"):
            worker = strict_from_json_value(WorkerSnapshot, data)
            self._workers[worker.worker_id] = worker.model_copy(
                update={
                    "online": False,
                    "gpus": tuple(
                        gpu.model_copy(update={"state": GpuState.UNKNOWN}) for gpu in worker.gpus
                    ),
                }
            )
        for data in self.events.iter_immutable("tasks"):
            loaded_task = strict_from_json_value(Task, data)
            self._tasks[loaded_task.task_id] = loaded_task
        latest_status: dict[str, TaskStatus] = {}
        initial_status: dict[str, TaskStatus] = {}
        for data in self.events.iter_snapshots("task-status"):
            status = strict_from_json_value(TaskStatus, data)
            latest_status[status.task_id] = status
        for data in self.events.iter_immutable("task-status-history"):
            status = strict_from_json_value(TaskStatus, data)
            prior = latest_status.get(status.task_id)
            if prior is None or status.updated_at >= prior.updated_at:
                latest_status[status.task_id] = status
            first = initial_status.get(status.task_id)
            if first is None or status.updated_at < first.updated_at:
                initial_status[status.task_id] = status
        self._initial_statuses = initial_status
        active_states = {
            TaskState.PREPARING,
            TaskState.RESERVED,
            TaskState.DISPATCHED,
            TaskState.STARTING,
            TaskState.RUNNING,
            TaskState.FINALIZING,
        }
        for task_id, status in list(latest_status.items()):
            if status.state in active_states:
                reconciled = status.model_copy(
                    update={
                        "state": TaskState.RECONCILIATION_REQUIRED,
                        "updated_at": utc_now(),
                        "failure_reason": "MASTER_RESTART_DURING_ACTIVE_EXECUTION",
                    }
                )
                latest_status[task_id] = reconciled
                self._persist_status(reconciled)
                self.events.append(
                    "tasks",
                    task_id,
                    "RECONCILIATION_REQUIRED",
                    "master-recovery",
                    new_id("request"),
                    {"previous_state": status.state.value},
                )
        self._statuses = latest_status
        for status in latest_status.values():
            queued_task = self._tasks.get(status.task_id)
            if queued_task and status.state in {TaskState.QUEUED, TaskState.BLOCKED}:
                self._queue[queued_task.task_id] = _Queued(
                    queued_task, status, status.updated_at, status.updated_at
                )
        for data in self.events.iter_immutable("plans"):
            plan = strict_from_json_value(ExecutionPlan, data)
            self._plans[plan.plan_id] = plan
            for gpu_id in plan.gpu_ids:
                resource = (plan.worker_id, f"gpu:{gpu_id}")
                self._epochs[resource] = max(self._epochs.get(resource, 0), plan.lease_epoch)
            container_resource = (plan.worker_id, f"container:{plan.container_name}")
            self._epochs[container_resource] = max(
                self._epochs.get(container_resource, 0), plan.lease_epoch
            )
        for data in self.events.iter_snapshots("leases"):
            lease = strict_from_json_value(ResourceLease, data)
            self._leases[lease.lease_id] = lease
        for data in self.events.iter_immutable("lease-history"):
            lease = strict_from_json_value(ResourceLease, data["lease"])
            for gpu_id in lease.gpu_ids:
                resource = (lease.worker_id, f"gpu:{gpu_id}")
                self._epochs[resource] = max(self._epochs.get(resource, 0), lease.lease_epoch)
            container_resource = (lease.worker_id, f"container:{lease.container_name}")
            self._epochs[container_resource] = max(
                self._epochs.get(container_resource, 0), lease.lease_epoch
            )
            if data.get("action") in {"HELD", "COMMITTED"}:
                self._leases[lease.lease_id] = lease
            elif data.get("action") == "RELEASED":
                self._leases.pop(lease.lease_id, None)
            else:
                raise SchedulingError("unknown Lease history action")

    @staticmethod
    def _sample_fresh(gpu: GpuSnapshot) -> bool:
        return (utc_now() - gpu.sampled_at).total_seconds() <= 30

    @staticmethod
    def _heartbeat_fresh(worker: WorkerSnapshot) -> bool:
        return (utc_now() - worker.last_heartbeat_at).total_seconds() <= 30


def _worker_object_id(worker_id: str) -> str:
    # Event object IDs remain schema-valid while retaining a stable worker mapping.
    import hashlib

    return f"worker_{hashlib.sha256(worker_id.encode()).hexdigest()[:32]}"


def _allowed_targets(state: TaskState) -> set[TaskState]:
    from agent_scheduler.domain.state import task_targets

    return task_targets(state)
