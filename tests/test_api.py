import io
import json

import httpx
import pytest
from conftest import proposal_markdown

from agent_scheduler.adapters.mcp import (
    MCPAdapterError,
    SubmitterMCPAdapter,
    _response_object,
)


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


def test_mcp_adapter_surfaces_control_plane_error_body():
    """A bare status line leaves the Submitter agent unable to correct its submission."""
    adapter = SubmitterMCPAdapter("https://example.invalid", "zz_chentian")
    request = httpx.Request("POST", "https://example.invalid/api/v1/proposals")
    response = httpx.Response(
        422,
        json={
            "error_code": "INVALID_PROPOSAL",
            "message": "qualification run command does not match frozen launcher",
        },
        request=request,
    )
    with pytest.raises(MCPAdapterError) as error:
        _response_object(response)
    message = str(error.value)
    assert "422" in message
    assert "INVALID_PROPOSAL" in message
    assert "frozen launcher" in message
    adapter.close()


def test_mcp_adapter_error_falls_back_to_body_text():
    request = httpx.Request("GET", "https://example.invalid/api/v1/proposals/prop_x")
    response = httpx.Response(502, text="upstream exploded", request=request)
    with pytest.raises(MCPAdapterError, match="upstream exploded"):
        _response_object(response)
