from datetime import timedelta
from pathlib import Path

import pytest
from conftest import signed_task

from agent_scheduler.domain.models import (
    GpuSnapshot,
    GpuState,
    TaskState,
    WorkerSnapshot,
    new_id,
    utc_now,
)
from agent_scheduler.integrity import sign_model
from agent_scheduler.scheduler import Scheduler, SchedulingError
from agent_scheduler.storage import EventStore
from agent_scheduler.worker import FakeWorkerDriver


def worker(vram: float = 0.0, state: GpuState = GpuState.AVAILABLE) -> WorkerSnapshot:
    now = utc_now()
    return WorkerSnapshot(
        worker_id="worker-local-01",
        online=True,
        last_heartbeat_at=now,
        gpus=tuple(
            GpuSnapshot(
                gpu_id=index,
                vram_percent=vram,
                sampled_at=now,
                state=state,
                raw_line="test",
            )
            for index in range(8)
        ),
    )


def named_worker(worker_id: str) -> WorkerSnapshot:
    return worker().model_copy(update={"worker_id": worker_id})


def scheduler_for(root: Path, identity, driver=None, threshold: float = 2.0):
    return Scheduler(
        EventStore(root),
        identity.signing_private_key,
        identity.signing_public_key,
        driver or FakeWorkerDriver(),
        key_id=identity.key_id,
        vram_threshold=threshold,
        qualification_profile=threshold > 2,
    )


def test_scheduler_runs_barriers_and_releases_lease(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity, 4)
    scheduler = scheduler_for(root, identity)
    scheduler.register_worker(worker())
    scheduler.enqueue(task)
    result = scheduler.tick()[0]
    assert result.state is TaskState.COMPLETED
    assert scheduler.leases() == []
    event_types = [
        event.event_type for event in EventStore(root).list_events("tasks", task.task_id)
    ]
    assert event_types == [
        "QUEUED",
        "PREPARING",
        "PREPARED",
        "RESERVED",
        "PLAN_ACK",
        "DISPATCHED",
        "STARTING",
        "RUNNING",
        "FINALIZING",
        "COMPLETED",
    ]


def test_scheduler_dispatches_one_task_across_two_workers(runtime_identity):
    root, identity = runtime_identity
    original = signed_task(identity)
    first = original.units[0].model_copy(update={"worker_id": "worker-a"})
    second = original.units[0].model_copy(
        update={"unit_id": new_id("unit"), "worker_id": "worker-b"}
    )
    task = sign_model(
        original.model_copy(
            update={"units": (first, second), "content_hash": None, "signature": None}
        ),
        identity.signing_private_key,
    )
    driver = FakeWorkerDriver()
    scheduler = scheduler_for(root, identity, driver)
    scheduler.register_worker(named_worker("worker-a"))
    scheduler.register_worker(named_worker("worker-b"))

    scheduler.enqueue(task)
    result = scheduler.tick()[0]

    assert result.state is TaskState.COMPLETED
    assert len(driver.executed) == 2
    plans = list(EventStore(root).iter_immutable("plans"))
    assert {plan["worker_id"] for plan in plans} == {"worker-a", "worker-b"}


def test_vram_gate_keeps_task_queued(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    scheduler = scheduler_for(root, identity, threshold=90)
    scheduler.register_worker(worker(91, GpuState.DRIFTED))
    scheduler.enqueue(task)
    assert scheduler.tick() == []
    assert scheduler.get_status(task.task_id).state is TaskState.QUEUED


def test_qualification_gpu_wait_expires_at_30_minutes(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    scheduler = scheduler_for(root, identity, threshold=90)
    scheduler.register_worker(worker(91, GpuState.DRIFTED))
    scheduler.enqueue(task)
    scheduler._queue[task.task_id].enqueued_at = utc_now() - timedelta(minutes=31)
    assert scheduler.tick() == []
    status = scheduler.get_status(task.task_id)
    assert status.state is TaskState.BLOCKED
    assert status.failure_reason == "QUALIFICATION_GPU_WAIT_EXPIRED"


def test_blocked_task_is_not_requeued_on_rebuild(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    scheduler = scheduler_for(root, identity, threshold=90)
    scheduler.register_worker(worker(91, GpuState.DRIFTED))
    scheduler.enqueue(task)
    scheduler._queue[task.task_id].enqueued_at = utc_now() - timedelta(minutes=31)
    scheduler.tick()
    assert scheduler.get_status(task.task_id).state is TaskState.BLOCKED
    rebuilt = scheduler_for(root, identity, threshold=90)
    rebuilt.register_worker(worker(91, GpuState.DRIFTED))
    # A re-queued BLOCKED task would expire again 30 minutes after the rebuild and
    # crash every tick with InvalidTransition (BLOCKED -> BLOCKED).
    assert rebuilt.queued_task_ids() == []
    assert rebuilt.tick() == []
    assert rebuilt.get_status(task.task_id).state is TaskState.BLOCKED


def test_expiry_skips_an_already_blocked_queue_entry(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    scheduler = scheduler_for(root, identity, threshold=90)
    scheduler.register_worker(worker(91, GpuState.DRIFTED))
    scheduler.enqueue(task)
    scheduler._transition(task, TaskState.BLOCKED, "test", failure_reason="manual")
    scheduler._queue[task.task_id].enqueued_at = utc_now() - timedelta(minutes=31)
    assert scheduler.tick() == []
    assert scheduler.get_status(task.task_id).state is TaskState.BLOCKED
    assert scheduler.queued_task_ids() == []


def test_cleanup_failure_retains_entire_lease(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    driver = FakeWorkerDriver(cleanup_ok=False)
    scheduler = scheduler_for(root, identity, driver)
    scheduler.register_worker(worker())
    scheduler.enqueue(task)
    result = scheduler.tick()[0]
    assert result.state is TaskState.CLEANUP_FAILED
    assert len(scheduler.leases()) == 1
    assert not scheduler.reconcile_execution(
        task.execution_id, actor="admin", reason="still running", request_id="req"
    )
    assert scheduler.leases()
    lease_id = scheduler.leases()[0].lease_id
    EventStore(root).delete_snapshot("leases", lease_id)
    rebuilt = scheduler_for(root, identity, FakeWorkerDriver(cleanup_ok=False))
    rebuilt.register_worker(worker())
    assert rebuilt.leases()[0].lease_id == lease_id
    second = signed_task(identity)
    rebuilt.enqueue(second)
    assert rebuilt.tick() == []
    assert rebuilt.get_status(second.task_id).state is TaskState.QUEUED


def test_tampered_task_is_rejected_before_prepare(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    driver = FakeWorkerDriver()
    scheduler = scheduler_for(root, identity, driver)
    scheduler.register_worker(worker())
    tampered = task.model_copy(update={"policy_version": "tampered"})
    with pytest.raises(SchedulingError, match="signature"):
        scheduler.enqueue(tampered)
    assert driver.prepared == []


def test_queued_task_can_be_cancelled(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    scheduler = scheduler_for(root, identity)
    scheduler.enqueue(task)
    status = scheduler.cancel_task(task.task_id, actor="zz_chentian", request_id="cancel-request")
    assert status.state is TaskState.CANCELLED
    assert scheduler.tick() == []


def test_active_task_restarts_in_reconciliation(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    first = scheduler_for(root, identity)
    first.enqueue(task)
    first._transition(task, TaskState.PREPARING, "test")
    rebuilt = scheduler_for(root, identity)
    assert rebuilt.get_status(task.task_id).state is TaskState.RECONCILIATION_REQUIRED


def test_queued_task_rebuilds_after_snapshot_deletion(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    first = scheduler_for(root, identity)
    first.enqueue(task)
    EventStore(root).delete_snapshot("task-status", task.task_id)
    rebuilt = scheduler_for(root, identity)
    rebuilt.register_worker(worker())
    assert rebuilt.tick()[0].state is TaskState.COMPLETED
