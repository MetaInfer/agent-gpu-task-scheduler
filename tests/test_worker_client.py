import json

import pytest
from conftest import signed_task

from agent_scheduler.domain.models import (
    ExecutionPlan,
    GpuSnapshot,
    GpuState,
    ProtocolEnvelope,
    new_id,
    utc_now,
)
from agent_scheduler.integrity import canonical_bytes, sign_model
from agent_scheduler.storage import EventStore
from agent_scheduler.worker.client import WorkerClient, WorkerClientError
from agent_scheduler.worker.docker import DockerCLI
from agent_scheduler.worker.driver import DockerWorkerDriver
from agent_scheduler.worker.gpu import HySmiSampler
from agent_scheduler.worker.protocol import RemoteWorkerDriver, WorkerHub


def test_worker_rereads_ground_truth_before_docker(runtime_identity):
    root, identity = runtime_identity
    store = EventStore(root)
    task = signed_task(identity)
    store.write_immutable("tasks", task.task_id, task)
    store.write_immutable_bytes("tasks", f"{task.task_id}.canonical.json", canonical_bytes(task))
    driver = DockerWorkerDriver(DockerCLI(), store, identity.signing_public_key, identity.key_id)
    client = WorkerClient(
        uri="wss://unused",
        worker_id="worker-local-01",
        api_key="unused",
        driver=driver,
        sampler=HySmiSampler(2),
        ca_file="unused",
    )
    envelope = ProtocolEnvelope(
        message_id=new_id("msg"),
        sequence=1,
        message_type="PREPARE",
        payload={
            "task_id": task.task_id,
            "task_content_hash": task.content_hash,
            "task_path": str(store.immutable_root / "tasks" / f"{task.task_id}.json"),
            "task_canonical_path": str(
                store.immutable_root / "tasks" / f"{task.task_id}.canonical.json"
            ),
        },
    )
    assert client._load_task(envelope) == task
    path = store.immutable_root / "tasks" / f"{task.task_id}.json"
    tampered = task.model_copy(update={"policy_version": "tampered"})
    path.write_text(json.dumps(tampered.model_dump(mode="json")) + "\n", encoding="utf-8")
    with pytest.raises(WorkerClientError):
        client._load_task(envelope)


def test_worker_rereads_plan_ground_truth_and_fencing(runtime_identity):
    root, identity = runtime_identity
    store = EventStore(root)
    task = signed_task(identity)
    unit = task.units[0]
    plan = sign_model(
        ExecutionPlan(
            key_id=identity.key_id,
            plan_id=new_id("plan"),
            assignment_id=new_id("assign"),
            execution_id=task.execution_id,
            task_id=task.task_id,
            task_content_hash=task.content_hash or "0" * 64,
            dispatch_generation=1,
            worker_id=unit.worker_id,
            unit_id=unit.unit_id,
            gpu_ids=(0,),
            lease_epoch=1,
            container_name=unit.container_name,
            submitter_username=unit.submitter_username,
            container_user=unit.container_user,
            image_digest=unit.image_digest,
            created_at=utc_now(),
        ),
        identity.signing_private_key,
    )
    store.write_immutable("plans", plan.plan_id, plan)
    store.write_immutable_bytes("plans", f"{plan.plan_id}.canonical.json", canonical_bytes(plan))
    driver = DockerWorkerDriver(DockerCLI(), store, identity.signing_public_key, identity.key_id)
    client = WorkerClient(
        uri="wss://unused",
        worker_id="worker-local-01",
        api_key="unused",
        driver=driver,
        sampler=HySmiSampler(2),
        ca_file="unused",
    )
    envelope = ProtocolEnvelope(
        message_id=new_id("msg"),
        sequence=1,
        message_type="PLAN",
        assignment_id=plan.assignment_id,
        dispatch_generation=plan.dispatch_generation,
        lease_epoch=plan.lease_epoch,
        payload={
            "plan_id": plan.plan_id,
            "plan_content_hash": plan.content_hash,
            "plan_path": str(store.immutable_root / "plans" / f"{plan.plan_id}.json"),
            "plan_canonical_path": str(
                store.immutable_root / "plans" / f"{plan.plan_id}.canonical.json"
            ),
        },
    )
    assert client._load_plan(envelope) == plan
    wrong_fence = envelope.model_copy(update={"lease_epoch": 2})
    with pytest.raises(WorkerClientError, match="fencing"):
        client._load_plan(wrong_fence)
    path = store.immutable_root / "plans" / f"{plan.plan_id}.json"
    path.write_text(
        json.dumps(plan.model_copy(update={"lease_epoch": 2}).model_dump(mode="json")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(WorkerClientError):
        client._load_plan(envelope)


def test_master_protocol_sequence_survives_restart(tmp_path):
    store = EventStore(tmp_path / "state")
    object_id = "worker_" + "0" * 32
    store.write_snapshot(
        "protocol-sequence", object_id, {"worker_id": "worker-local-01", "sequence": 42}
    )
    hub = WorkerHub(store, lambda _worker: None)
    assert hub._sequence["worker-local-01"] == 42


def test_remote_driver_routes_each_plan_to_its_worker(runtime_identity):
    root, identity = runtime_identity
    task = signed_task(identity)
    unit = task.units[0]
    plan = sign_model(
        ExecutionPlan(
            key_id=identity.key_id,
            plan_id=new_id("plan"),
            assignment_id=new_id("assign"),
            execution_id=task.execution_id,
            task_id=task.task_id,
            task_content_hash=task.content_hash or "0" * 64,
            dispatch_generation=1,
            worker_id="worker-remote-02",
            unit_id=unit.unit_id,
            gpu_ids=(0,),
            lease_epoch=1,
            container_name=unit.container_name,
            submitter_username=unit.submitter_username,
            container_user=unit.container_user,
            image_digest=unit.image_digest,
            created_at=utc_now(),
        ),
        identity.signing_private_key,
    )

    class Hub:
        def __init__(self):
            self.events = EventStore(root)
            self.worker_ids = []

        def request(self, worker_id, *_args, **_kwargs):
            self.worker_ids.append(worker_id)
            return ProtocolEnvelope(
                message_id=new_id("msg"),
                sequence=1,
                message_type="PLAN_RESULT",
                payload={"accepted": True},
            )

    hub = Hub()
    driver = RemoteWorkerDriver(hub, str(root))

    assert driver.acknowledge_plan(plan, task)
    assert hub.worker_ids == ["worker-remote-02"]


def test_unacked_worker_response_survives_restart(runtime_identity):
    root, identity = runtime_identity
    store = EventStore(root)
    driver = DockerWorkerDriver(DockerCLI(), store, identity.signing_public_key, identity.key_id)
    first = WorkerClient(
        uri="wss://unused",
        worker_id="worker-local-01",
        api_key="unused",
        driver=driver,
        sampler=HySmiSampler(2),
        ca_file="unused",
    )
    original_id = new_id("msg")
    response = ProtocolEnvelope(
        message_id=new_id("msg"),
        sequence=1,
        message_type="EXECUTE_RESULT",
        payload={"reply_to": original_id, "exit_code": 0},
    )
    first._record_response(original_id, response)
    rebuilt = WorkerClient(
        uri="wss://unused",
        worker_id="worker-local-01",
        api_key="unused",
        driver=driver,
        sampler=HySmiSampler(2),
        ca_file="unused",
    )
    assert rebuilt._responses[original_id] == response
    rebuilt._ack_response(response.message_id)
    assert original_id not in rebuilt._responses
    assert not (rebuilt._protocol_dir / "responses" / f"{original_id}.json").exists()


@pytest.mark.asyncio
async def test_heartbeat_sampling_failure_is_logged_and_retried(
    runtime_identity, monkeypatch, caplog
):
    root, identity = runtime_identity
    store = EventStore(root)
    driver = DockerWorkerDriver(DockerCLI(), store, identity.signing_public_key, identity.key_id)

    class Sampler:
        calls = 0

        def sample(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient hy-smi failure")
            now = utc_now()
            return (
                GpuSnapshot(
                    gpu_id=0,
                    vram_percent=0.0,
                    sampled_at=now,
                    state=GpuState.AVAILABLE,
                    raw_line="test",
                ),
            )

    class WebSocket:
        def __init__(self):
            self.sent = []

        async def send(self, value):
            self.sent.append(value)
            raise StopAsyncIteration

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr("agent_scheduler.worker.client.asyncio.sleep", no_delay)
    sampler = Sampler()
    client = WorkerClient(
        uri="wss://unused",
        worker_id="worker-local-01",
        api_key="unused",
        driver=driver,
        sampler=sampler,
        ca_file="unused",
    )
    websocket = WebSocket()

    with (
        caplog.at_level("ERROR", logger="agent_scheduler.worker"),
        pytest.raises(StopAsyncIteration),
    ):
        await client._heartbeats(websocket)

    assert sampler.calls == 2
    assert len(websocket.sent) == 1
    assert "heartbeat will retry" in caplog.text
