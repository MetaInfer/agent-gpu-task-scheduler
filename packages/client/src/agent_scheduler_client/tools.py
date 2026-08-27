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
