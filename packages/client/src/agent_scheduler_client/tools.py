"""Canonical Submitter MCP tool definitions."""

from __future__ import annotations

SUBMITTER_TOOLS = (
    "create_proposal",
    "reply",
    "confirm_revision",
    "get_reviews",
    "resume",
    "cancel",
    "get_proposal",
    "get_task",
    "cancel_task",
    "wait_for_task",
    "wait_for_events",
    "get_logs",
)

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


def build_tools(username: str) -> list[dict[str, object]]:
    return [
        _tool(
            "create_proposal",
            (
                "Create a Proposal through the authoritative REST control plane. "
                f"The configured submitter username is `{username}`; use it exactly "
                "in the Proposal Identity section."
            ),
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
        _tool(
            "get_proposal", "Read current Proposal state.", {"proposal_id": _ID}, ["proposal_id"]
        ),
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


def validate_tool_arguments(name: str, arguments: dict[str, object]) -> None:
    """Enforce the same closed schemas exposed by ``tools/list``."""
    tool = next((value for value in build_tools("") if value["name"] == name), None)
    if tool is None:
        raise ValueError(f"unknown tool: {name}")
    schema = tool["inputSchema"]
    if not isinstance(schema, dict):
        raise TypeError("tool input schema must be an object")
    properties = schema.get("properties")
    required = schema.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise TypeError("tool input schema properties and required must be collections")
    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        raise ValueError(f"unexpected tool arguments: {unexpected}")
    missing = [value for value in required if isinstance(value, str) and value not in arguments]
    if missing:
        raise ValueError(f"missing required tool arguments: {missing}")
    for argument_name, value in arguments.items():
        constraint = properties[argument_name]
        if not isinstance(constraint, dict):
            raise TypeError(f"tool constraint must be an object: {argument_name}")
        expected_type = constraint.get("type")
        if expected_type == "string":
            if not isinstance(value, str):
                raise TypeError(f"{argument_name} must be a string")
            if constraint.get("minLength") == 1 and not value:
                raise ValueError(f"{argument_name} must not be empty")
        elif expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{argument_name} must be an integer")
            minimum = constraint.get("minimum")
            maximum = constraint.get("maximum")
            if isinstance(minimum, int) and value < minimum:
                raise ValueError(f"{argument_name} must be at least {minimum}")
            if isinstance(maximum, int) and value > maximum:
                raise ValueError(f"{argument_name} must be between {minimum} and {maximum}")
        else:
            raise TypeError(f"unsupported tool constraint type: {expected_type!r}")
