import json

import pytest
from conftest import signed_task

from agent_scheduler.domain.models import ProtocolEnvelope, new_id
from agent_scheduler.integrity import canonical_bytes
from agent_scheduler.storage import EventStore
from agent_scheduler.worker.client import WorkerClient, WorkerClientError
from agent_scheduler.worker.docker import DockerCLI
from agent_scheduler.worker.driver import DockerWorkerDriver
from agent_scheduler.worker.gpu import HySmiSampler


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
