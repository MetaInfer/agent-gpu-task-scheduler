import io
import json

import httpx
import pytest
from agent_scheduler_client.mcp import SubmitterMCPAdapter
from agent_scheduler_client.rest import MCPAdapterError, SubmitterRESTClient, _response_object
from agent_scheduler_client.tools import SUBMITTER_TOOLS


def test_mcp_stdio_surface():
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


def test_tool_list_exposes_configured_username_without_accepting_it_as_input():
    adapter = SubmitterMCPAdapter("https://example.invalid", "client_user-1")
    incoming = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    )
    outgoing = io.StringIO()

    adapter.run_stdio(incoming, outgoing)
    adapter.close()

    response = json.loads(outgoing.getvalue())
    tools = response["result"]["tools"]
    assert tuple(tool["name"] for tool in tools) == SUBMITTER_TOOLS
    create = next(tool for tool in tools if tool["name"] == "create_proposal")
    assert "client_user-1" in create["description"]
    assert "username" not in create["inputSchema"]["properties"]
    assert all("username" not in tool["inputSchema"]["properties"] for tool in tools)


def test_rest_client_fixes_identity_and_idempotency_headers():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={"proposal": {"proposal_id": "prop_test"}})

    http = httpx.Client(
        base_url="https://master.example",
        transport=httpx.MockTransport(handler),
    )
    client = SubmitterRESTClient(
        "https://master.example",
        "client_user-1",
        client=http,
    )

    result = client.create_proposal("# Proposal", "create-test-1")
    client.close()

    assert result["proposal"] == {"proposal_id": "prop_test"}
    assert requests[0].url.path == "/api/v1/proposals"
    assert requests[0].headers["X-Username"] == "client_user-1"
    assert requests[0].headers["Idempotency-Key"] == "create-test-1"
