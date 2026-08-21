"""Submitter MCP stdio Adapter; REST remains the only authority."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

import httpx


class MCPAdapterError(RuntimeError):
    pass


@dataclass
class SubmitterMCPAdapter:
    base_url: str
    username: str
    verify: bool | str = True
    _client: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=self.base_url,
            verify=self.verify,
            timeout=httpx.Timeout(connect=10, read=15 * 60, write=30, pool=30),
        )

    def close(self) -> None:
        self._client.close()

    def create_proposal(self, markdown: str, idempotency_key: str) -> dict[str, object]:
        return self._post(
            "/api/v1/proposals",
            {"markdown": markdown},
            idempotency_key=idempotency_key,
        )

    def reply(self, proposal_id: str, markdown: str, idempotency_key: str) -> dict[str, object]:
        return self._post(
            f"/api/v1/proposals/{proposal_id}/replies",
            {"markdown": markdown},
            idempotency_key=idempotency_key,
        )

    def confirm(
        self, proposal_id: str, revision_id: str, idempotency_key: str
    ) -> dict[str, object]:
        return self._post(
            f"/api/v1/proposals/{proposal_id}/confirm",
            {"revision_id": revision_id},
            idempotency_key=idempotency_key,
        )

    def get_reviews(self, proposal_id: str) -> dict[str, object]:
        return self._get(f"/api/v1/proposals/{proposal_id}/reviews")

    def resume(self, proposal_id: str, idempotency_key: str) -> dict[str, object]:
        return self._post(
            f"/api/v1/proposals/{proposal_id}/resume", {}, idempotency_key=idempotency_key
        )

    def cancel(self, proposal_id: str) -> dict[str, object]:
        return self._post(f"/api/v1/proposals/{proposal_id}/cancel", {})

    def get_proposal(self, proposal_id: str) -> dict[str, object]:
        return self._get(f"/api/v1/proposals/{proposal_id}")

    def get_task(self, task_id: str) -> dict[str, object]:
        return self._get(f"/api/v1/tasks/{task_id}")

    def cancel_task(self, task_id: str) -> dict[str, object]:
        return self._post(f"/api/v1/tasks/{task_id}/cancel", {})

    def wait_for_task(self, task_id: str, timeout_seconds: int = 30) -> dict[str, object]:
        deadline = time.monotonic() + min(max(timeout_seconds, 1), 30)
        terminal = {"BLOCKED", "COMPLETED", "FAILED", "CANCELLED", "CLEANUP_FAILED"}
        while True:
            value = self.get_task(task_id)
            status = value.get("status")
            if isinstance(status, dict) and status.get("state") in terminal:
                return value
            if time.monotonic() >= deadline:
                return value
            time.sleep(1)

    def wait_for_events(
        self, proposal_id: str, after_sequence: int, timeout_seconds: int = 30
    ) -> dict[str, object]:
        deadline = time.monotonic() + min(max(timeout_seconds, 1), 30)
        while True:
            value = self._get(
                f"/api/v1/proposals/{proposal_id}/events",
                params={"after_sequence": after_sequence},
            )
            events = value.get("events")
            if isinstance(events, list) and events:
                return value
            if time.monotonic() >= deadline:
                return value
            time.sleep(1)

    def get_logs(
        self,
        task_id: str,
        unit_id: str,
        execution_id: str,
        name: str,
        offset: int = 0,
    ) -> dict[str, object]:
        response = self._client.get(
            f"/api/v1/logs/{task_id}/{unit_id}/{execution_id}/{name}",
            params={"offset": offset},
            headers=self._headers(),
        )
        _raise_for_status(response)
        return {
            "data": response.text,
            "next_offset": int(response.headers.get("X-Next-Offset", offset)),
        }

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, object]:
        operations: dict[str, Callable[[], dict[str, object]]] = {
            "create_proposal": lambda: self.create_proposal(
                _string(arguments, "markdown"), _string(arguments, "idempotency_key")
            ),
            "reply": lambda: self.reply(
                _string(arguments, "proposal_id"),
                _string(arguments, "markdown"),
                _string(arguments, "idempotency_key"),
            ),
            "confirm_revision": lambda: self.confirm(
                _string(arguments, "proposal_id"),
                _string(arguments, "revision_id"),
                _string(arguments, "idempotency_key"),
            ),
            "get_reviews": lambda: self.get_reviews(_string(arguments, "proposal_id")),
            "resume": lambda: self.resume(
                _string(arguments, "proposal_id"), _string(arguments, "idempotency_key")
            ),
            "cancel": lambda: self.cancel(_string(arguments, "proposal_id")),
            "get_proposal": lambda: self.get_proposal(_string(arguments, "proposal_id")),
            "get_task": lambda: self.get_task(_string(arguments, "task_id")),
            "cancel_task": lambda: self.cancel_task(_string(arguments, "task_id")),
            "wait_for_task": lambda: self.wait_for_task(
                _string(arguments, "task_id"),
                _integer(arguments, "timeout_seconds", 30),
            ),
            "wait_for_events": lambda: self.wait_for_events(
                _string(arguments, "proposal_id"),
                _integer(arguments, "after_sequence", 0),
                _integer(arguments, "timeout_seconds", 30),
            ),
            "get_logs": lambda: self.get_logs(
                _string(arguments, "task_id"),
                _string(arguments, "unit_id"),
                _string(arguments, "execution_id"),
                _string(arguments, "name"),
                _integer(arguments, "offset", 0),
            ),
        }
        try:
            operation = operations[name]
        except KeyError as exc:
            raise MCPAdapterError(f"unknown tool: {name}") from exc
        return operation()

    def run_stdio(
        self, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout
    ) -> None:
        for line in input_stream:
            if not line.strip():
                continue
            request_id: object = None
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise MCPAdapterError("JSON-RPC message must be an object")
                request_id = message.get("id")
                method = message.get("method")
                if method == "notifications/initialized":
                    continue
                if method == "initialize":
                    params = message.get("params", {})
                    protocol = (
                        params.get("protocolVersion", "2025-11-25")
                        if isinstance(params, dict)
                        else "2025-11-25"
                    )
                    result: dict[str, object] = {
                        "protocolVersion": protocol,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "agent-scheduler-submitter", "version": "0.2.0"},
                    }
                elif method == "tools/list":
                    result = {"tools": _TOOLS}
                elif method == "tools/call":
                    params = message.get("params")
                    if not isinstance(params, dict) or not isinstance(params.get("name"), str):
                        raise MCPAdapterError("tools/call requires name and arguments")
                    arguments = params.get("arguments", {})
                    if not isinstance(arguments, dict):
                        raise MCPAdapterError("tool arguments must be an object")
                    value = self.call_tool(params["name"], arguments)
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(value, sort_keys=True, ensure_ascii=False),
                            }
                        ],
                        "isError": False,
                    }
                else:
                    raise MCPAdapterError(f"unsupported method: {method}")
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except (MCPAdapterError, httpx.HTTPError, json.JSONDecodeError, TypeError) as exc:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            output_stream.write(json.dumps(response, separators=(",", ":")) + "\n")
            output_stream.flush()

    def _post(
        self,
        path: str,
        body: dict[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        response = self._client.post(path, json=body, headers=self._headers(idempotency_key))
        return _response_object(response)

    def _get(
        self,
        path: str,
        *,
        params: dict[str, str | int | float | bool | None] | None = None,
    ) -> dict[str, object]:
        return _response_object(self._client.get(path, params=params, headers=self._headers()))

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"X-Username": self.username}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers


def _raise_for_status(response: httpx.Response) -> None:
    """Surface the control plane's structured error body.

    ``httpx.raise_for_status`` reports only the status line, which leaves the Submitter
    agent unable to tell a validation rejection from a genuine conflict. The control
    plane always answers errors with ``{"error_code": ..., "message": ...}``, so relay
    both instead of discarding them.
    """
    if not response.is_error:
        return
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = body.get("error_code")
        message = body.get("message") or body.get("detail")
        detail = " ".join(str(part) for part in (code, message) if part)
    if not detail:
        detail = response.text.strip()[:500]
    raise MCPAdapterError(
        f"{response.request.method} {response.request.url} failed with HTTP "
        f"{response.status_code}: {detail or 'no error body'}"
    )


def _response_object(response: httpx.Response) -> dict[str, object]:
    _raise_for_status(response)
    value = response.json()
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError("REST response must be a JSON object")
    return value


def _string(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise MCPAdapterError(f"{name} must be a string")
    return value


def _integer(arguments: dict[str, Any], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MCPAdapterError(f"{name} must be an integer")
    return value


_ID: dict[str, object] = {"type": "string", "minLength": 1}
_KEY: dict[str, object] = {"type": "string", "minLength": 1}


def _tool(
    name: str,
    description: str,
    properties: dict[str, object],
    required: list[str],
) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


_TOOLS = [
    _tool(
        "create_proposal",
        "Create a Proposal through the authoritative REST control plane.",
        {"markdown": _KEY, "idempotency_key": _KEY},
        ["markdown", "idempotency_key"],
    ),
    _tool(
        "reply",
        "Submit a complete revised Proposal reply.",
        {"proposal_id": _ID, "markdown": _KEY, "idempotency_key": _KEY},
        ["proposal_id", "markdown", "idempotency_key"],
    ),
    _tool(
        "confirm_revision",
        "Explicitly confirm the current immutable revision for review.",
        {"proposal_id": _ID, "revision_id": _ID, "idempotency_key": _KEY},
        ["proposal_id", "revision_id", "idempotency_key"],
    ),
    _tool(
        "get_reviews",
        "Read Reviewer decisions, rationale, and current normalized Facts.",
        {"proposal_id": _ID},
        ["proposal_id"],
    ),
    _tool(
        "resume",
        "Resume a recoverable Proposal state.",
        {"proposal_id": _ID, "idempotency_key": _KEY},
        ["proposal_id", "idempotency_key"],
    ),
    _tool("cancel", "Cancel a non-terminal Proposal.", {"proposal_id": _ID}, ["proposal_id"]),
    _tool("get_proposal", "Read current Proposal state.", {"proposal_id": _ID}, ["proposal_id"]),
    _tool("get_task", "Read immutable Task and current status.", {"task_id": _ID}, ["task_id"]),
    _tool("cancel_task", "Cancel a queued or running Task.", {"task_id": _ID}, ["task_id"]),
    _tool(
        "wait_for_task",
        "Poll Task state for at most 30 seconds.",
        {
            "task_id": _ID,
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ["task_id"],
    ),
    _tool(
        "wait_for_events",
        "Poll Proposal events for at most 30 seconds.",
        {
            "proposal_id": _ID,
            "after_sequence": {"type": "integer", "minimum": 0},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ["proposal_id", "after_sequence"],
    ),
    _tool(
        "get_logs",
        "Read Framework log bytes from an offset.",
        {
            "task_id": _ID,
            "unit_id": _ID,
            "execution_id": _ID,
            "name": _KEY,
            "offset": {"type": "integer", "minimum": 0},
        },
        ["task_id", "unit_id", "execution_id", "name"],
    ),
]
