from conftest import proposal_markdown

from agent_scheduler.worker.docker import DockerError


def test_proposal_confirm_schedule_and_observe(app_client):
    client, _app = app_client
    response = client.post(
        "/api/v1/proposals",
        headers={"X-Username": "zz_chentian", "Idempotency-Key": "create-api-1"},
        json={"markdown": proposal_markdown(1)},
    )
    assert response.status_code == 201
    proposal = response.json()["proposal"]
    confirmed = client.post(
        f"/api/v1/proposals/{proposal['proposal_id']}/confirm",
        headers={
            "X-Username": "zz_chentian",
            "Idempotency-Key": "confirm-api-1",
        },
        json={"revision_id": proposal["current_revision_id"]},
    )
    assert confirmed.status_code == 200
    task = confirmed.json()["task"]
    assert task["content_hash"] and task["signature"]
    tick = client.post("/api/v1/admin/tick", json={"actor": "tester", "reason": "run test"})
    assert tick.status_code == 200
    assert tick.json()["statuses"][0]["state"] == "COMPLETED"
    fetched = client.get(f"/api/v1/tasks/{task['task_id']}")
    assert fetched.json()["status"]["state"] == "COMPLETED"
    repeated = client.post(
        f"/api/v1/proposals/{proposal['proposal_id']}/confirm",
        headers={
            "X-Username": "zz_chentian",
            "Idempotency-Key": "confirm-api-1",
        },
        json={"revision_id": proposal["current_revision_id"]},
    )
    assert repeated.json()["status"]["state"] == "QUEUED"
    summary = client.get("/api/v1/observe/summary")
    assert summary.status_code == 200
    assert summary.json()["proposals"]
    assert summary.json()["reviews"]
    assert summary.json()["tasks"]
    assert summary.json()["units"]
    assert "queue" in summary.json()
    assert summary.json()["containers"]
    assert "audit_streams" in summary.json()
    assert client.post("/api/v1/observe/summary").status_code == 405
    assert "worker_api_key" not in summary.text


def test_idempotency_conflict_and_strict_request(app_client):
    client, _app = app_client
    headers = {"X-Username": "zz_chentian", "Idempotency-Key": "conflict-api-1"}
    assert (
        client.post(
            "/api/v1/proposals",
            headers=headers,
            json={"markdown": proposal_markdown(1)},
        ).status_code
        == 201
    )
    response = client.post(
        "/api/v1/proposals", headers=headers, json={"markdown": proposal_markdown(2)}
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "IDEMPOTENCY_CONFLICT"
    unknown = client.post(
        "/api/v1/proposals",
        headers={"X-Username": "zz_chentian", "Idempotency-Key": "unknown"},
        json={"markdown": proposal_markdown(), "extra": True},
    )
    assert unknown.status_code == 422
    assert set(unknown.json()).issuperset(
        {
            "error_code",
            "message",
            "object_id",
            "current_state",
            "request_id",
            "retryable",
        }
    )
    method = client.post("/api/v1/observe/summary")
    assert method.status_code == 405
    assert method.json()["error_code"] == "METHOD_NOT_ALLOWED"


def test_cancel_and_drain_are_real_state_changes(app_client):
    client, _app = app_client
    created = client.post(
        "/api/v1/proposals",
        headers={"X-Username": "zz_chentian", "Idempotency-Key": "cancel-api"},
        json={"markdown": proposal_markdown()},
    ).json()["proposal"]
    cancelled = client.post(
        f"/api/v1/proposals/{created['proposal_id']}/cancel",
        headers={"X-Username": "zz_chentian"},
    )
    assert cancelled.json()["proposal"]["state"] == "CANCELLED"
    drained = client.post("/api/v1/admin/drain", json={"actor": "tester", "reason": "maintenance"})
    assert drained.json()["draining"] is True
    rejected = client.post(
        "/api/v1/proposals",
        headers={"X-Username": "zz_chentian", "Idempotency-Key": "after-drain"},
        json={"markdown": proposal_markdown()},
    )
    assert rejected.status_code == 503


def test_dashboard_surface(app_client):
    client, _app = app_client
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'id="pipeline"' in dashboard.text
    assert 'id="gpus"' in dashboard.text
    assert 'id="alerts"' in dashboard.text
    assert "2000" in dashboard.text


def test_summary_log_indexes_are_counted_not_enumerated(app_client):
    """Both indexes grow without bound, so the summary must not walk and return them whole."""
    client, _app = app_client
    body = client.get("/api/v1/observe/summary").json()
    for key in ("framework_logs", "audit_streams"):
        index = body[key]
        assert isinstance(index, dict), key
        assert isinstance(index["count"], int), key
        assert isinstance(index["recent"], list), key
        assert len(index["recent"]) <= 20, key


def test_summary_caches_docker_inspection(app_client, monkeypatch):
    """A 2s dashboard poll must not shell out to `docker inspect` on every request."""
    from agent_scheduler.api import app as app_module

    calls = []

    def fake_inspect(self, name):
        calls.append(name)
        raise DockerError("no docker in tests")

    monkeypatch.setattr(app_module.DockerCLI, "inspect", fake_inspect)
    app_module._CONTAINER_CACHE.clear()

    client, _app = app_client
    for _ in range(4):
        assert client.get("/api/v1/observe/summary").status_code == 200
    assert len(calls) == 1
