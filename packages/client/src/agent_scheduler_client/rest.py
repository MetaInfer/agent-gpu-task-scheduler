"""REST-backed Submitter client core."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx


class MCPAdapterError(RuntimeError):
    pass


class SubmitterRESTClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        verify: bool | str = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.verify = verify
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url,
            verify=verify,
            timeout=httpx.Timeout(connect=10, read=15 * 60, write=30, pool=30),
        )

    def close(self) -> None:
        if self._owns_client:
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

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
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


def _string(arguments: dict[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str):
        raise MCPAdapterError(f"{name} must be a string")
    return value


def _integer(arguments: dict[str, object], name: str, default: int) -> int:
    value = arguments.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise MCPAdapterError(f"{name} must be an integer")
    return value
