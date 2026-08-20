import io
import json

from conftest import proposal_markdown

from agent_scheduler.adapters.mcp import SubmitterMCPAdapter


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
    summary = client.get("/api/v1/observe/summary")
    assert summary.status_code == 200
    assert summary.json()["proposals"]
    assert summary.json()["tasks"]
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
    assert response.json()["detail"]["error_code"] == "IDEMPOTENCY_CONFLICT"
    unknown = client.post(
        "/api/v1/proposals",
        headers={"X-Username": "zz_chentian", "Idempotency-Key": "unknown"},
        json={"markdown": proposal_markdown(), "extra": True},
    )
    assert unknown.status_code == 422


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


def test_dashboard_and_mcp_stdio_surface(app_client):
    client, _app = app_client
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "每 5 秒刷新" in dashboard.text

    adapter = SubmitterMCPAdapter("https://example.invalid", "zz_chentian")
    incoming = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    outgoing = io.StringIO()
    adapter.run_stdio(incoming, outgoing)
    adapter.close()
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert responses[0]["result"]["serverInfo"]["version"] == "0.2.0"
    names = {tool["name"] for tool in responses[1]["result"]["tools"]}
    assert names == {
        "create_proposal",
        "reply",
        "confirm_revision",
        "resume",
        "cancel",
        "get_proposal",
        "get_reviews",
        "get_task",
        "cancel_task",
        "wait_for_task",
        "wait_for_events",
        "get_logs",
    }
