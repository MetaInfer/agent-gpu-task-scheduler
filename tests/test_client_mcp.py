import io
import json
from typing import Any

import httpx
import pytest
from agent_scheduler_client.mcp import SubmitterMCPAdapter
from agent_scheduler_client.rest import MCPAdapterError, SubmitterRESTClient, _response_object
from agent_scheduler_client.tools import SUBMITTER_TOOLS, build_tools

_TOOL_CASES: tuple[tuple[str, dict[str, object], str, str, object, dict[str, str]], ...] = (
    (
        "create_proposal",
        {"markdown": "# Proposal", "idempotency_key": "key-1"},
        "POST",
        "/api/v1/proposals",
        {"markdown": "# Proposal"},
        {"Idempotency-Key": "key-1"},
    ),
    (
        "reply",
        {"proposal_id": "prop_1", "markdown": "# Revised", "idempotency_key": "key-2"},
        "POST",
        "/api/v1/proposals/prop_1/replies",
        {"markdown": "# Revised"},
        {"Idempotency-Key": "key-2"},
    ),
    (
        "confirm_revision",
        {"proposal_id": "prop_1", "revision_id": "rev_1", "idempotency_key": "key-3"},
        "POST",
        "/api/v1/proposals/prop_1/confirm",
        {"revision_id": "rev_1"},
        {"Idempotency-Key": "key-3"},
    ),
    ("get_reviews", {"proposal_id": "prop_1"}, "GET", "/api/v1/proposals/prop_1/reviews", None, {}),
    (
        "resume",
        {"proposal_id": "prop_1", "idempotency_key": "key-4"},
        "POST",
        "/api/v1/proposals/prop_1/resume",
        {},
        {"Idempotency-Key": "key-4"},
    ),
    ("cancel", {"proposal_id": "prop_1"}, "POST", "/api/v1/proposals/prop_1/cancel", {}, {}),
    ("get_proposal", {"proposal_id": "prop_1"}, "GET", "/api/v1/proposals/prop_1", None, {}),
    ("get_task", {"task_id": "task_1"}, "GET", "/api/v1/tasks/task_1", None, {}),
    ("cancel_task", {"task_id": "task_1"}, "POST", "/api/v1/tasks/task_1/cancel", {}, {}),
    (
        "wait_for_task",
        {"task_id": "task_1", "timeout_seconds": 1},
        "GET",
        "/api/v1/tasks/task_1",
        None,
        {},
    ),
    (
        "wait_for_events",
        {"proposal_id": "prop_1", "after_sequence": 7, "timeout_seconds": 1},
        "GET",
        "/api/v1/proposals/prop_1/events?after_sequence=7",
        None,
        {},
    ),
    (
        "get_logs",
        {
            "task_id": "task_1",
            "unit_id": "unit_1",
            "execution_id": "exec_1",
            "name": "framework.log",
            "offset": 9,
        },
        "GET",
        "/api/v1/logs/task_1/unit_1/exec_1/framework.log?offset=9",
        None,
        {},
    ),
)


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
    incoming = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
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


def test_submitter_tool_names_are_ordered_and_unique() -> None:
    names = [tool["name"] for tool in build_tools("client_user-1")]
    assert names == list(SUBMITTER_TOOLS)
    assert len(names) == len(set(names))


def test_tool_schemas_are_exact_and_closed() -> None:
    expected = {
        name: {
            "type": "object",
            "properties": arguments,
            "required": required,
            "additionalProperties": False,
        }
        for name, arguments, required in (
            (
                "create_proposal",
                {
                    "markdown": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                ["markdown", "idempotency_key"],
            ),
            (
                "reply",
                {
                    "proposal_id": {"type": "string", "minLength": 1},
                    "markdown": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                ["proposal_id", "markdown", "idempotency_key"],
            ),
            (
                "confirm_revision",
                {
                    "proposal_id": {"type": "string", "minLength": 1},
                    "revision_id": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                ["proposal_id", "revision_id", "idempotency_key"],
            ),
            ("get_reviews", {"proposal_id": {"type": "string", "minLength": 1}}, ["proposal_id"]),
            (
                "resume",
                {
                    "proposal_id": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                ["proposal_id", "idempotency_key"],
            ),
            ("cancel", {"proposal_id": {"type": "string", "minLength": 1}}, ["proposal_id"]),
            ("get_proposal", {"proposal_id": {"type": "string", "minLength": 1}}, ["proposal_id"]),
            ("get_task", {"task_id": {"type": "string", "minLength": 1}}, ["task_id"]),
            ("cancel_task", {"task_id": {"type": "string", "minLength": 1}}, ["task_id"]),
            (
                "wait_for_task",
                {
                    "task_id": {"type": "string", "minLength": 1},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                ["task_id"],
            ),
            (
                "wait_for_events",
                {
                    "proposal_id": {"type": "string", "minLength": 1},
                    "after_sequence": {"type": "integer", "minimum": 0},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
                },
                ["proposal_id", "after_sequence"],
            ),
            (
                "get_logs",
                {
                    "task_id": {"type": "string", "minLength": 1},
                    "unit_id": {"type": "string", "minLength": 1},
                    "execution_id": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "offset": {"type": "integer", "minimum": 0},
                },
                ["task_id", "unit_id", "execution_id", "name"],
            ),
        )
    }
    tools = build_tools("client_user-1")
    assert [tool["name"] for tool in tools] == list(SUBMITTER_TOOLS)
    assert {tool["name"]: tool["inputSchema"] for tool in tools} == expected
    for tool in tools:
        name = tool["name"]
        schema = tool["inputSchema"]
        assert isinstance(name, str) and isinstance(schema, dict)
        properties = schema["properties"]
        expected_properties = expected[name]["properties"]
        assert isinstance(properties, dict) and isinstance(expected_properties, dict)
        assert list(properties) == list(expected_properties)


@pytest.mark.parametrize(
    ("name", "arguments", "method", "url", "body", "extra_headers"),
    _TOOL_CASES,
)
def test_all_semantic_tools_use_the_exact_rest_contract(
    name: str,
    arguments: dict[str, object],
    method: str,
    url: str,
    body: object,
    extra_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"X-Next-Offset": "12"} if name == "get_logs" else {}
        if name == "wait_for_task":
            payload: object = {"status": {"state": "COMPLETED"}}
        elif name == "wait_for_events":
            payload = {"events": [{"sequence": 8}]}
        elif name == "get_logs":
            return httpx.Response(200, text="log", headers=headers)
        else:
            payload = {"ok": True}
        return httpx.Response(200, json=payload, headers=headers)

    monkeypatch.setattr("agent_scheduler_client.rest.time.sleep", lambda _seconds: None)
    http = httpx.Client(base_url="https://master.example", transport=httpx.MockTransport(handler))
    client = SubmitterRESTClient("https://master.example", "client_user-1", client=http)

    result = client.call_tool(name, arguments)

    assert len(requests) == 1
    request = requests[0]
    assert request.method == method
    assert request.url.raw_path.decode() == url
    assert request.headers["X-Username"] == "client_user-1"
    assert {key: request.headers[key] for key in extra_headers} == extra_headers
    if not extra_headers:
        assert "Idempotency-Key" not in request.headers
    assert (json.loads(request.content) if request.content else None) == body
    expected_result: object
    if name == "get_logs":
        expected_result = {"data": "log", "next_offset": 12}
    elif name == "wait_for_task":
        expected_result = {"status": {"state": "COMPLETED"}}
    elif name == "wait_for_events":
        expected_result = {"events": [{"sequence": 8}]}
    else:
        expected_result = {"ok": True}
    assert result == expected_result


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("create_proposal", {"markdown": 1, "idempotency_key": "key"}, "markdown must be a string"),
        (
            "reply",
            {"proposal_id": [], "markdown": "x", "idempotency_key": "key"},
            "proposal_id must be a string",
        ),
        (
            "confirm_revision",
            {"proposal_id": "p", "revision_id": False, "idempotency_key": "key"},
            "revision_id must be a string",
        ),
        ("get_reviews", {"proposal_id": None}, "proposal_id must be a string"),
        ("resume", {"proposal_id": "p", "idempotency_key": 2}, "idempotency_key must be a string"),
        ("cancel", {"proposal_id": 2}, "proposal_id must be a string"),
        ("get_proposal", {"proposal_id": {}}, "proposal_id must be a string"),
        ("get_task", {"task_id": 2}, "task_id must be a string"),
        ("cancel_task", {"task_id": []}, "task_id must be a string"),
        (
            "wait_for_task",
            {"task_id": "t", "timeout_seconds": True},
            "timeout_seconds must be an integer",
        ),
        (
            "wait_for_events",
            {"proposal_id": "p", "after_sequence": "0"},
            "after_sequence must be an integer",
        ),
        (
            "get_logs",
            {"task_id": "t", "unit_id": "u", "execution_id": "e", "name": "n", "offset": 1.5},
            "offset must be an integer",
        ),
    ],
)
def test_all_semantic_tools_reject_wrong_argument_types(
    name: str, arguments: dict[str, object], message: str
) -> None:
    client = SubmitterRESTClient("https://master.example", "client_user-1")
    try:
        with pytest.raises(MCPAdapterError, match=message):
            client.call_tool(name, arguments)
    finally:
        client.close()


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        (
            "create_proposal",
            {"markdown": "", "idempotency_key": "key"},
            "markdown must not be empty",
        ),
        (
            "get_task",
            {"task_id": "task", "unexpected": "value"},
            "unexpected tool arguments",
        ),
        (
            "wait_for_task",
            {"task_id": "task", "timeout_seconds": 31},
            "timeout_seconds must be between 1 and 30",
        ),
        (
            "wait_for_events",
            {"proposal_id": "proposal", "after_sequence": -1},
            "after_sequence must be at least 0",
        ),
        (
            "get_logs",
            {
                "task_id": "task",
                "unit_id": "unit",
                "execution_id": "execution",
                "name": "log",
                "offset": -1,
            },
            "offset must be at least 0",
        ),
    ],
)
def test_tool_calls_enforce_closed_schema_value_constraints(
    name: str, arguments: dict[str, object], message: str
) -> None:
    client = SubmitterRESTClient("https://master.example", "client_user-1")
    try:
        with pytest.raises(MCPAdapterError, match=message):
            client.call_tool(name, arguments)
    finally:
        client.close()


@pytest.mark.parametrize("header", ["not-an-integer", "-1", "8"])
def test_get_logs_rejects_malformed_or_inconsistent_next_offset(header: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="abc", headers={"X-Next-Offset": header})

    http = httpx.Client(base_url="https://master.example", transport=httpx.MockTransport(handler))
    client = SubmitterRESTClient("https://master.example", "client_user-1", client=http)
    with pytest.raises(MCPAdapterError, match="X-Next-Offset"):
        client.get_logs("task", "unit", "execution", "name", 7)


def test_malformed_log_header_emits_one_error_then_processes_next_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="abc", headers={"X-Next-Offset": "broken"}, request=request)

    http = httpx.Client(base_url="https://master.example", transport=httpx.MockTransport(handler))
    rest = SubmitterRESTClient("https://master.example", "client_user-1", client=http)
    adapter = SubmitterMCPAdapter("https://master.example", "client_user-1", rest_client=rest)
    incoming = io.StringIO(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_logs",
                    "arguments": {
                        "task_id": "task",
                        "unit_id": "unit",
                        "execution_id": "execution",
                        "name": "framework.log",
                    },
                },
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    outgoing = io.StringIO()

    adapter.run_stdio(incoming, outgoing)

    responses: list[dict[str, Any]] = [
        json.loads(line) for line in outgoing.getvalue().splitlines()
    ]
    assert len(responses) == 2
    assert responses[0]["id"] == 1 and "X-Next-Offset" in responses[0]["error"]["message"]
    assert responses[1]["id"] == 2
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == list(SUBMITTER_TOOLS)


def test_wait_for_events_clamps_timeout_without_real_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = iter([10.0, 40.0])
    monkeypatch.setattr("agent_scheduler_client.rest.time.monotonic", lambda: next(now))
    monkeypatch.setattr("agent_scheduler_client.rest.time.sleep", lambda _seconds: None)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"events": []})

    http = httpx.Client(base_url="https://master.example", transport=httpx.MockTransport(handler))
    client = SubmitterRESTClient("https://master.example", "client_user-1", client=http)

    assert client.wait_for_events("proposal", 3, timeout_seconds=999) == {"events": []}
    assert len(requests) == 1
    assert requests[0].url.raw_path.decode().endswith("?after_sequence=3")


def test_polling_clamps_timeout_and_stops_at_every_terminal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = iter([0.0, 30.0])
    monkeypatch.setattr("agent_scheduler_client.rest.time.monotonic", lambda: next(now))
    monkeypatch.setattr("agent_scheduler_client.rest.time.sleep", lambda _seconds: None)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": {"state": "RUNNING"}})

    http = httpx.Client(base_url="https://master.example", transport=httpx.MockTransport(handler))
    client = SubmitterRESTClient("https://master.example", "client_user-1", client=http)
    client.wait_for_task("task", timeout_seconds=999)
    assert calls == 1
    monkeypatch.setattr("agent_scheduler_client.rest.time.monotonic", lambda: 0.0)

    for state in ("BLOCKED", "COMPLETED", "FAILED", "CANCELLED", "CLEANUP_FAILED"):
        terminal_http = httpx.Client(
            base_url="https://master.example",
            transport=httpx.MockTransport(
                lambda _request, state=state: httpx.Response(200, json={"status": {"state": state}})
            ),
        )
        SubmitterRESTClient(
            "https://master.example", "client_user-1", client=terminal_http
        ).wait_for_task("task", timeout_seconds=-5)


def test_injected_http_client_is_not_owned() -> None:
    http = httpx.Client(
        base_url="https://master.example",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    client = SubmitterRESTClient("https://master.example", "client_user-1", client=http)
    client.close()
    assert not http.is_closed
    http.close()


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(
                409,
                json={"error_code": "CHANGES_REQUESTED", "message": "revise"},
                request=httpx.Request("POST", "https://master.example/api/v1/proposals/p/confirm"),
            ),
            "CHANGES_REQUESTED revise",
        ),
        (
            httpx.Response(
                502,
                text="x" * 600,
                request=httpx.Request("GET", "https://master.example/api/v1/tasks/t"),
            ),
            "x" * 500,
        ),
    ],
)
def test_structured_and_unstructured_errors_are_bounded(
    response: httpx.Response, expected: str
) -> None:
    with pytest.raises(MCPAdapterError) as error:
        _response_object(response)
    assert expected in str(error.value)
    assert "x" * 501 not in str(error.value)
