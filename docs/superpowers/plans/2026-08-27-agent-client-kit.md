# Agent Client Kit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and verify a versioned Client Kit that lets Claude Code, Codex CLI, pi, and dsh submit GPU tasks without receiving or importing the scheduler's server-side source package.

**Architecture:** Move the existing Submitter MCP implementation into a standalone `agent_scheduler_client` package, split its REST, tool-schema, stdio, and CLI responsibilities, and have the server-side qualification fixtures consume that same package through a compatibility wrapper. Build an allowlisted Client Kit around the resulting wheel, canonical skill, four-harness configuration templates, and a standalone client document; prove isolation by inspecting the wheel and running it from a workspace where `agent_scheduler` cannot be imported.

**Tech Stack:** Python 3.10+, hatchling/PEP 517 wheels, httpx, stdlib JSON-RPC/argparse/venv/zipfile/hashlib, pytest, ruff, mypy strict, JSON/TOML/YAML configuration consumed by Claude Code/Codex CLI/pi/dsh.

**Spec:** `docs/superpowers/specs/2026-08-27-agent-client-kit-design.md`

## Global Constraints

- The public distribution is named `agent-gpu-task-scheduler-client`; its import package is `agent_scheduler_client`; its console entrypoint is `agent-scheduler-submitter`.
- Python support remains `>=3.10`; the client runtime dependency is `httpx>=0.27,<1` and its transitive dependencies only.
- The client wheel must contain `agent_scheduler_client/**` and must not contain `agent_scheduler/**`, server prompts, server config, tests, runtime identity code, or private repository paths.
- The server wheel may bundle `agent_scheduler_client`; server and client distributions are mutually exclusive in one environment.
- `agent-scheduler-submitter` requires explicit `--base-url`, `--username`, and `--ca-file`; HTTPS verification cannot be disabled.
- MCP stdout contains JSON-RPC only; all diagnostics go to stderr.
- The public tool count remains exactly 12. Username is added only to the dynamic `create_proposal` description, never to a tool input schema.
- The canonical skill uses semantic tool names and `<submitter-username>`; it must not tell a client to start Master or Worker.
- Client configuration and docs may use deployment tokens `@@CLIENT_ENTRYPOINT@@`, `@@MASTER_URL@@`, `@@USERNAME@@`, `@@CA_FILE@@`, and `@@CLIENT_WORKSPACE@@`; no other unresolved token is accepted.
- Living project documentation uses direct `python3` commands, never `uv`.
- Default tests must not invoke a real Agent, model API, Master deployment, Worker, or GPU. Existing `RUN_REAL_*` opt-ins remain mandatory.
- Never write user-level Agent configuration; generated qualification configuration stays under its per-run output directory.
- Preserve unrelated user changes. Before every commit, inspect `git status --short` and stage only files owned by that task.
- Every commit message ends with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

---

## Planned File Structure

### New client package

- `packages/client/pyproject.toml` — metadata and wheel boundary for the client-only distribution.
- `packages/client/src/agent_scheduler_client/__init__.py` — client version export.
- `packages/client/src/agent_scheduler_client/__main__.py` — `python3 -m agent_scheduler_client` entrypoint.
- `packages/client/src/agent_scheduler_client/cli.py` — hardened client CLI and process lifecycle.
- `packages/client/src/agent_scheduler_client/mcp.py` — stdio JSON-RPC handling only.
- `packages/client/src/agent_scheduler_client/rest.py` — Master REST methods, polling, and error propagation.
- `packages/client/src/agent_scheduler_client/tools.py` — canonical tool names and dynamic schemas.
- `packages/client/wheelhouse-requirements.txt` — pinned pure-Python runtime wheel set used by release builds.

### New delivery assets

- `config/client/mcp.example.json` — shared Claude Code/pi stdio config template.
- `config/client/codex-mcp.example.toml` — Codex reference config template.
- `config/client/dsh-mcp.example.patch.yml` — dsh MCP plus skill-filesystem patch template.
- `docs/submitting-from-an-agent-client.md` — standalone client-only installation and operation guide.
- `src/agent_scheduler/client_kit.py` — allowlisted Kit assembly, validation, manifest, hashes, and offline smoke install.
- `scripts/build_client_kit.py` — thin command-line wrapper around `agent_scheduler.client_kit`.

### New focused tests

- `tests/test_client_mcp.py` — tool descriptions, JSON-RPC, REST calls, and errors.
- `tests/test_client_cli.py` — CLI validation, stream discipline, and lifecycle.
- `tests/test_client_package.py` — source layout, build metadata, and optional built-wheel isolation checks.
- `tests/test_client_kit.py` — allowlisted Kit assembly, manifest, hashes, tokens, and symlink rejection.
- `tests/test_client_docs.py` — standalone client-doc requirements and forbidden provider operations.
- `tests/test_client_isolation.py` — built wheel in an empty venv/workspace against a live fake Master.

### Existing files changed or removed

- `pyproject.toml` — bundle the client package in the server wheel, expose the client console script, and teach pytest/mypy both source roots.
- `src/agent_scheduler/adapters/mcp.py` — moved and then removed; no compatibility reimplementation remains here.
- `src/agent_scheduler/cli/main.py` — internal `mcp` wrapper delegates to client CLI with the public certificate.
- `src/agent_scheduler/adapters/onboarding.py` — renders client-entrypoint/CA configs without source cwd or state-root env.
- `src/agent_scheduler/adapters/submitter.py` — prepares an isolated client workspace and returns its cwd.
- `src/agent_scheduler/qualification.py` — launches the harness from that isolated workspace and audits the same cwd.
- `.agents/skills/submit-gpu-task/SKILL.md` — remove provider operations and harness-specific tool prefixes.
- `.agents/skills/submit-gpu-task/reference/proposal-template.md` — replace the hard-coded username.
- `docs/submitting-from-an-agent-session.md` — mark as provider-internal.
- `README.md` — link both provider and client guides with explicit audiences.
- `docs/testing-the-submitter.md` — document that T2/T3 now exercise the client package from an isolated workspace.
- `tests/test_api.py`, `tests/test_cli.py`, `tests/test_onboarding.py`, `tests/test_submitter_harness.py`, `tests/test_qualification.py`, `tests/test_real_onboarding.py`, and `tests/test_real_qualification.py` — update imports, signatures, cwd assertions, and real-test setup.

---

### Task 1: Establish the client distribution and move the MCP implementation once

**Files:**
- Create: `packages/client/pyproject.toml`
- Create: `packages/client/src/agent_scheduler_client/__init__.py`
- Move: `src/agent_scheduler/adapters/mcp.py` → `packages/client/src/agent_scheduler_client/mcp.py`
- Modify: `pyproject.toml:25-52`
- Modify: `src/agent_scheduler/cli/main.py:14-16`
- Modify: `tests/test_api.py:8-12`
- Modify: `tests/test_cli.py:5-6`
- Create: `tests/test_client_package.py`
- Track: `docs/superpowers/specs/2026-08-27-agent-client-kit-design.md`
- Track: `docs/superpowers/plans/2026-08-27-agent-client-kit.md`

**Interfaces:**
- Consumes: current `SubmitterMCPAdapter`, `MCPAdapterError`, and `_response_object` behavior from `src/agent_scheduler/adapters/mcp.py`.
- Produces: importable `agent_scheduler_client`, `agent_scheduler_client.__version__ == "0.2.0"`, and the same adapter symbols at `agent_scheduler_client.mcp`.

- [ ] **Step 1: Write the failing package-boundary test**

Create `tests/test_client_package.py` with the initial source-boundary assertion:

```python
from pathlib import Path

from agent_scheduler_client import __version__
from agent_scheduler_client.mcp import SubmitterMCPAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_client_adapter_has_one_source_location():
    assert __version__ == "0.2.0"
    assert SubmitterMCPAdapter.__module__ == "agent_scheduler_client.mcp"
    assert (
        PROJECT_ROOT
        / "packages"
        / "client"
        / "src"
        / "agent_scheduler_client"
        / "mcp.py"
    ).is_file()
    assert not (PROJECT_ROOT / "src" / "agent_scheduler" / "adapters" / "mcp.py").exists()
```

Update the MCP imports in `tests/test_api.py` and `tests/test_cli.py` to import from `agent_scheduler_client.mcp`. Do not alter assertions yet.

- [ ] **Step 2: Run the test and verify the new package does not exist**

Run:

```bash
python3 -m pytest tests/test_client_package.py tests/test_api.py -k 'client_adapter or mcp_adapter or mcp_stdio' -v
```

Expected: collection fails with `ModuleNotFoundError: No module named 'agent_scheduler_client'`.

- [ ] **Step 3: Add the client package metadata and source root**

Create `packages/client/pyproject.toml`:

```toml
[project]
name = "agent-gpu-task-scheduler-client"
version = "0.2.0"
description = "Client-only Submitter MCP adapter for Agent GPU Task Scheduler"
requires-python = ">=3.10"
dependencies = [
  "httpx>=0.27,<1",
]

[build-system]
requires = ["hatchling>=1.25"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/agent_scheduler_client"]
```

Create `packages/client/src/agent_scheduler_client/__init__.py`:

```python
"""Client-only Submitter MCP adapter."""

__version__ = "0.2.0"
```

Modify the root `pyproject.toml` wheel and tool roots:

```toml
[tool.hatch.build.targets.wheel]
packages = [
  "src/agent_scheduler",
  "packages/client/src/agent_scheduler_client",
]

[tool.pytest.ini_options]
pythonpath = ["src", "packages/client/src"]

[tool.mypy]
python_version = "3.10"
strict = true
packages = ["agent_scheduler", "agent_scheduler_client"]
mypy_path = ["src", "packages/client/src"]
```

Keep the existing pytest markers and ruff settings unchanged.

- [ ] **Step 4: Move the implementation and switch the server import**

Run:

```bash
mkdir -p packages/client/src/agent_scheduler_client
git mv src/agent_scheduler/adapters/mcp.py packages/client/src/agent_scheduler_client/mcp.py
```

In the moved module, import `__version__` and replace the literal server version:

```python
from agent_scheduler_client import __version__
```

```python
"serverInfo": {"name": "agent-scheduler-submitter", "version": __version__},
```

In `src/agent_scheduler/cli/main.py`, replace the old adapter import with:

```python
from agent_scheduler_client.mcp import SubmitterMCPAdapter
```

Update all remaining imports found by:

```bash
rg -n 'agent_scheduler\.adapters\.mcp' src tests
```

The command must print no matches after the edit.

- [ ] **Step 5: Run focused and full source checks**

Run:

```bash
python3 -m pytest tests/test_client_package.py tests/test_api.py tests/test_cli.py -v
python3 -m ruff check packages/client/src src tests/test_client_package.py
python3 -m mypy src packages/client/src
```

Expected: all commands pass; no file remains at `src/agent_scheduler/adapters/mcp.py`.

- [ ] **Step 6: Commit the single-source move and approved design artifacts**

Run:

```bash
git status --short
git add pyproject.toml packages/client src/agent_scheduler/cli/main.py \
  tests/test_api.py tests/test_cli.py tests/test_client_package.py \
  docs/superpowers/specs/2026-08-27-agent-client-kit-design.md \
  docs/superpowers/plans/2026-08-27-agent-client-kit.md
git commit -m "$(cat <<'EOF'
refactor: establish the client-only package boundary

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Split REST, tool schemas, and stdio while exposing configured identity

**Files:**
- Create: `packages/client/src/agent_scheduler_client/rest.py`
- Create: `packages/client/src/agent_scheduler_client/tools.py`
- Modify: `packages/client/src/agent_scheduler_client/mcp.py`
- Modify: `packages/client/src/agent_scheduler_client/__init__.py`
- Modify: `tests/test_api.py:122-193`
- Create: `tests/test_client_mcp.py`

**Interfaces:**
- Consumes: `agent_scheduler_client.mcp.SubmitterMCPAdapter` from Task 1.
- Produces:
  - `SUBMITTER_TOOLS: tuple[str, ...]`
  - `build_tools(username: str) -> list[dict[str, object]]`
  - `SubmitterRESTClient(base_url: str, username: str, verify: bool | str = True, client: httpx.Client | None = None)`
  - `SubmitterMCPAdapter(base_url: str, username: str, verify: bool | str = True, rest_client: SubmitterRESTClient | None = None)`
  - `MCPAdapterError` and `_response_object()` in `agent_scheduler_client.rest`.

- [ ] **Step 1: Move MCP-specific tests out of the API test file and add identity assertions**

Create `tests/test_client_mcp.py`. Move the three existing MCP tests from `tests/test_api.py` into it, import `MCPAdapterError` and `_response_object` from `agent_scheduler_client.rest`, import `SubmitterMCPAdapter` from `agent_scheduler_client.mcp`, then add:

```python
import io
import json

from agent_scheduler_client.mcp import SubmitterMCPAdapter
from agent_scheduler_client.tools import SUBMITTER_TOOLS


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
```

Add a REST-path test using `httpx.MockTransport`:

```python
import httpx

from agent_scheduler_client.rest import SubmitterRESTClient


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
```

- [ ] **Step 2: Run the tests and verify the split interfaces are missing**

Run:

```bash
python3 -m pytest tests/test_client_mcp.py -v
```

Expected: collection fails because `agent_scheduler_client.tools` and `agent_scheduler_client.rest` do not exist.

- [ ] **Step 3: Create the canonical tool module**

Create `packages/client/src/agent_scheduler_client/tools.py` with:

```python
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
```

- [ ] **Step 4: Extract the REST implementation without changing endpoint behavior**

Create `packages/client/src/agent_scheduler_client/rest.py`. Move `MCPAdapterError`, every REST method from the old adapter (`create_proposal` through `get_logs`), `call_tool`, `_post`, `_get`, `_headers`, `_raise_for_status`, `_response_object`, `_string`, and `_integer` into `SubmitterRESTClient` and module helpers.

Use this constructor and ownership rule exactly:

```python
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
```

Preserve the current endpoint paths, bodies, terminal states, timeout clamps, and 500-character fallback error limit exactly. An injected test client is not closed by `SubmitterRESTClient`.

- [ ] **Step 5: Reduce the MCP adapter to JSON-RPC and composition**

Modify `agent_scheduler_client/mcp.py` so it imports:

```python
from agent_scheduler_client import __version__
from agent_scheduler_client.rest import MCPAdapterError, SubmitterRESTClient
from agent_scheduler_client.tools import build_tools
```

Use this constructor and delegation boundary:

```python
class SubmitterMCPAdapter:
    def __init__(
        self,
        base_url: str,
        username: str,
        verify: bool | str = True,
        rest_client: SubmitterRESTClient | None = None,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.verify = verify
        self._rest = rest_client or SubmitterRESTClient(base_url, username, verify)

    def close(self) -> None:
        self._rest.close()

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return self._rest.call_tool(name, arguments)
```

Keep `run_stdio()` in this module. Replace its `tools/list` branch with:

```python
result = {"tools": build_tools(self.username)}
```

Remove all REST methods and `_TOOLS` from `mcp.py`; do not retain forwarding copies beyond `call_tool()` and `close()`.

Export the stable surface from `agent_scheduler_client/__init__.py`:

```python
"""Client-only Submitter MCP adapter."""

__version__ = "0.2.0"

__all__ = ["__version__"]
```

- [ ] **Step 6: Run client, API, lint, and type checks**

Run:

```bash
python3 -m pytest tests/test_client_mcp.py tests/test_api.py -v
python3 -m ruff check packages/client/src tests/test_client_mcp.py tests/test_api.py
python3 -m mypy packages/client/src
```

Expected: all pass; `tests/test_api.py` contains only REST API/dashboard tests, not Adapter tests.

- [ ] **Step 7: Commit the transport-independent client core**

Run:

```bash
git status --short
git add packages/client/src/agent_scheduler_client tests/test_api.py tests/test_client_mcp.py
git commit -m "$(cat <<'EOF'
refactor: split the submitter client core

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Add the hardened public CLI and preserve the internal wrapper

**Files:**
- Create: `packages/client/src/agent_scheduler_client/cli.py`
- Create: `packages/client/src/agent_scheduler_client/__main__.py`
- Modify: `packages/client/pyproject.toml`
- Modify: `pyproject.toml:25-27`
- Modify: `src/agent_scheduler/cli/main.py:45-48,129-139`
- Modify: `tests/test_cli.py:1-44`
- Create: `tests/test_client_cli.py`

**Interfaces:**
- Consumes: `SubmitterMCPAdapter` from Task 2.
- Produces:
  - `build_parser() -> argparse.ArgumentParser`
  - `run_mcp(*, base_url: str, username: str, ca_file: Path, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout) -> int`
  - `main(argv: list[str] | None = None) -> int`
  - console command `agent-scheduler-submitter` in both distribution metadata files.

- [ ] **Step 1: Write CLI validation and lifecycle tests**

Create `tests/test_client_cli.py`:

```python
import io
from pathlib import Path

import pytest

from agent_scheduler_client import cli


def test_cli_requires_https_username_and_readable_ca(tmp_path: Path):
    ca_file = tmp_path / "certificate.pem"
    ca_file.write_text("public certificate", encoding="ascii")

    args = cli.build_parser().parse_args(
        [
            "--base-url",
            "https://master.example:8443",
            "--username",
            "client_user-1",
            "--ca-file",
            str(ca_file),
        ]
    )

    assert args.base_url == "https://master.example:8443"
    assert args.username == "client_user-1"
    assert args.ca_file == ca_file


@pytest.mark.parametrize(
    "base_url",
    [
        "http://master.example:8443",
        "https://user@master.example:8443",
        "https://master.example:8443?debug=1",
        "https://master.example:8443/#fragment",
    ],
)
def test_cli_rejects_unsafe_base_urls(base_url: str, tmp_path: Path):
    ca_file = tmp_path / "certificate.pem"
    ca_file.write_text("public certificate", encoding="ascii")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "--base-url",
                base_url,
                "--username",
                "client_user-1",
                "--ca-file",
                str(ca_file),
            ]
        )


@pytest.mark.parametrize("username", ["", "has space", "slash/user", "x" * 65])
def test_cli_rejects_invalid_usernames(username: str, tmp_path: Path):
    ca_file = tmp_path / "certificate.pem"
    ca_file.write_text("public certificate", encoding="ascii")
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "--base-url",
                "https://master.example:8443",
                "--username",
                username,
                "--ca-file",
                str(ca_file),
            ]
        )


def test_run_mcp_passes_explicit_ca_and_closes_adapter(tmp_path: Path, monkeypatch):
    ca_file = tmp_path / "certificate.pem"
    ca_file.write_text("public certificate", encoding="ascii")
    events: list[object] = []

    class FakeAdapter:
        def __init__(self, base_url: str, username: str, verify: str):
            events.append((base_url, username, verify))

        def run_stdio(self, input_stream, output_stream):
            events.append((input_stream.read(), output_stream))

        def close(self):
            events.append("closed")

    monkeypatch.setattr(cli, "_adapter_type", lambda: FakeAdapter)
    incoming = io.StringIO("")
    outgoing = io.StringIO()

    assert (
        cli.run_mcp(
            base_url="https://master.example:8443",
            username="client_user-1",
            ca_file=ca_file,
            input_stream=incoming,
            output_stream=outgoing,
        )
        == 0
    )
    assert events[0] == (
        "https://master.example:8443",
        "client_user-1",
        str(ca_file),
    )
    assert events[-1] == "closed"
    assert outgoing.getvalue() == ""
```

- [ ] **Step 2: Run tests and verify the CLI module is missing**

Run:

```bash
python3 -m pytest tests/test_client_cli.py -v
```

Expected: collection fails because `agent_scheduler_client.cli` does not exist.

- [ ] **Step 3: Implement strict argument types and lazy protocol startup**

Create `packages/client/src/agent_scheduler_client/cli.py` with stdlib-only runtime imports at module import time. Keep `--help` usable when runtime dependencies have not yet been installed by importing `SubmitterMCPAdapter` only inside a small loader that tests can replace:

```python
"""Console entrypoint for the client-only Submitter MCP adapter."""

from __future__ import annotations

import argparse
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, TextIO
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from agent_scheduler_client.mcp import SubmitterMCPAdapter

_USERNAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise argparse.ArgumentTypeError(
            "base URL must be HTTPS with a host and no userinfo, query, or fragment"
        )
    return value.rstrip("/")


def _username(value: str) -> str:
    if not _USERNAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "username must match [A-Za-z0-9_.-]{1,64}"
        )
    return value


def _readable_file(value: str) -> Path:
    path = Path(value)
    if not path.is_file() or not os.access(path, os.R_OK):
        raise argparse.ArgumentTypeError(f"CA file is not a readable regular file: {path}")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-scheduler-submitter")
    parser.add_argument("--base-url", required=True, type=_https_url)
    parser.add_argument("--username", required=True, type=_username)
    parser.add_argument("--ca-file", required=True, type=_readable_file)
    return parser


def _adapter_type() -> type[SubmitterMCPAdapter]:
    from agent_scheduler_client.mcp import SubmitterMCPAdapter

    return SubmitterMCPAdapter


def run_mcp(
    *,
    base_url: str,
    username: str,
    ca_file: Path,
    input_stream: TextIO = sys.stdin,
    output_stream: TextIO = sys.stdout,
) -> int:
    adapter = _adapter_type()(base_url, username, verify=str(ca_file))
    try:
        adapter.run_stdio(input_stream, output_stream)
    finally:
        adapter.close()
    return 0


def _terminate(_signum: int, _frame: FrameType | None) -> None:
    raise SystemExit(143)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    previous: Callable[[int, FrameType | None], object] | int | None = None
    if hasattr(signal, "SIGTERM"):
        previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        return run_mcp(
            base_url=args.base_url,
            username=args.username,
            ca_file=args.ca_file,
        )
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
```

Keep `_adapter_type()` lazy so `--help` works before `httpx` is installed. If mypy rejects the signal handler union, introduce a named `SignalHandler` alias matching `signal.signal` instead of adding `Any`.

Create `__main__.py`:

```python
from agent_scheduler_client.cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Register both console entrypoints and keep the server wrapper**

Add to `packages/client/pyproject.toml`:

```toml
[project.scripts]
agent-scheduler-submitter = "agent_scheduler_client.cli:main"
```

Add to the root `[project.scripts]`:

```toml
agent-scheduler = "agent_scheduler.cli.main:main"
agent-scheduler-submitter = "agent_scheduler_client.cli:main"
```

In `src/agent_scheduler/cli/main.py`, import:

```python
from agent_scheduler_client.cli import run_mcp
```

Keep the internal `mcp` parser's current `--base-url` and `--username` interface. Replace its runtime branch with:

```python
if args.command == "mcp":
    if not args.username:
        raise SystemExit("mcp requires --username or AGENT_SCHEDULER_USERNAME")
    settings = Settings.from_env()
    return run_mcp(
        base_url=args.base_url,
        username=args.username,
        ca_file=load_tls_certificate(settings.state_root),
    )
```

No server secret loader is introduced.

- [ ] **Step 5: Update the compatibility-wrapper test**

In `tests/test_cli.py`, remove the Adapter monkeypatch. Monkeypatch `cli_main.run_mcp` and assert exact values:

```python
def fake_run_mcp(*, base_url, username, ca_file):
    started.update(
        base_url=base_url,
        username=username,
        ca_file=ca_file,
    )
    return 0

monkeypatch.setattr(cli_main, "run_mcp", fake_run_mcp)
```

Keep the existing assertion that `load_runtime` is never called, then assert:

```python
assert started == {
    "base_url": "https://127.0.0.1:8443",
    "username": "zz_chentian",
    "ca_file": root / "tls" / "certificate.pem",
}
```

- [ ] **Step 6: Run the CLI and package checks**

Run:

```bash
python3 -m pytest tests/test_client_cli.py tests/test_cli.py -v
PYTHONPATH=packages/client/src python3 -m agent_scheduler_client --help
python3 -m ruff check packages/client/src tests/test_client_cli.py tests/test_cli.py
python3 -m mypy packages/client/src src/agent_scheduler/cli
```

Expected: all pass; help prints `--base-url`, `--username`, and `--ca-file` to stdout without trying to connect.

- [ ] **Step 7: Commit the public CLI and compatibility wrapper**

Run:

```bash
git status --short
git add pyproject.toml packages/client/pyproject.toml \
  packages/client/src/agent_scheduler_client/cli.py \
  packages/client/src/agent_scheduler_client/__main__.py \
  src/agent_scheduler/cli/main.py tests/test_client_cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
feat: add the client submitter entrypoint

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Render source-free onboarding configs and ship all three templates

**Files:**
- Create: `config/client/mcp.example.json`
- Create: `config/client/codex-mcp.example.toml`
- Create: `config/client/dsh-mcp.example.patch.yml`
- Modify: `src/agent_scheduler/adapters/onboarding.py`
- Modify: `tests/test_onboarding.py`

**Interfaces:**
- Consumes: `SUBMITTER_TOOLS` from `agent_scheduler_client.tools` and the console entrypoint from Task 3.
- Produces:

```python
build_onboarding(
    harness: str,
    *,
    output_dir: Path,
    workspace: Path,
    base_url: str,
    username: str,
    client_entrypoint: Path,
    ca_file: Path,
) -> OnboardingConfig
```

- [ ] **Step 1: Rewrite onboarding tests around the public command contract**

Change the `_build()` helper in `tests/test_onboarding.py` to:

```python
def _build(harness: str, tmp_path: Path):
    return build_onboarding(
        harness,
        output_dir=tmp_path / "run",
        workspace=tmp_path / "workspace",
        base_url="https://master.example:8443",
        username="client_user-1",
        client_entrypoint=Path("/opt/agent-client/venv/bin/agent-scheduler-submitter"),
        ca_file=Path("/shared/state/tls/certificate.pem"),
    )
```

Replace the old common-command assertion with:

```python
assert "agent-scheduler-submitter" in rendered
assert "agent_scheduler.cli.main" not in rendered
assert "AGENT_SCHEDULER_STATE_ROOT" not in rendered
assert "/public/share/fh/agent-gpu-task-scheduler" not in rendered
assert "--ca-file" in rendered
```

Update Claude/pi expectations so both JSON variants include `directTools`; update Codex to expect three `-c` flags (command, args, cwd), not four.

Add template tests:

```python
import tomllib


_TEMPLATE_TOKENS = {
    "@@CLIENT_ENTRYPOINT@@",
    "@@MASTER_URL@@",
    "@@USERNAME@@",
    "@@CA_FILE@@",
    "@@CLIENT_WORKSPACE@@",
}


def test_client_config_templates_use_only_documented_tokens():
    paths = (
        PROJECT_ROOT / "config" / "client" / "mcp.example.json",
        PROJECT_ROOT / "config" / "client" / "codex-mcp.example.toml",
        PROJECT_ROOT / "config" / "client" / "dsh-mcp.example.patch.yml",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        found = {f"@@{value}@@" for value in text.split("@@")[1::2]}
        assert found == _TEMPLATE_TOKENS
        assert "agent_scheduler.cli.main" not in text
        assert "AGENT_SCHEDULER_STATE_ROOT" not in text


def test_json_and_toml_templates_parse_before_rendering():
    json.loads(
        (PROJECT_ROOT / "config" / "client" / "mcp.example.json").read_text(
            encoding="utf-8"
        )
    )
    tomllib.loads(
        (PROJECT_ROOT / "config" / "client" / "codex-mcp.example.toml").read_text(
            encoding="utf-8"
        )
    )
```

For the YAML file, assert the exact directive/package/config key lines in T1; Task 10's real dsh `--dump-config`/T2 run is the authoritative parser check and remains opt-in.

- [ ] **Step 2: Run onboarding tests and verify the old signature fails**

Run:

```bash
python3 -m pytest tests/test_onboarding.py -v
```

Expected: `_build()` fails because `build_onboarding()` does not accept `workspace`, `client_entrypoint`, or `ca_file`.

- [ ] **Step 3: Refactor `build_onboarding()` to emit the client command**

In `onboarding.py`, import:

```python
from agent_scheduler_client.tools import SUBMITTER_TOOLS
```

Remove the local `SUBMITTER_TOOLS` tuple. Change `build_onboarding()` to the interface above and build:

```python
args = [
    "--base-url",
    base_url,
    "--username",
    username,
    "--ca-file",
    str(ca_file),
]
server: dict[str, object] = {
    "command": str(client_entrypoint),
    "args": args,
    "cwd": str(workspace),
    "directTools": list(SUBMITTER_TOOLS),
}
```

Use the same server shape for Claude and pi. Claude returns strict MCP config argv; pi additionally returns `PI_CODING_AGENT_DIR` under the run output directory. Codex emits only command/args/cwd `-c` overrides. dsh's `_dsh_patch()` receives `workspace` and renders:

```text
customSkillDirs:
  - "<workspace>/.agents/skills"
```

Remove all state-root env generation and all project-root cwd use from this module.

- [ ] **Step 4: Add exact generic client templates**

Create `config/client/mcp.example.json`:

```json
{
  "mcpServers": {
    "submitter": {
      "command": "@@CLIENT_ENTRYPOINT@@",
      "args": [
        "--base-url",
        "@@MASTER_URL@@",
        "--username",
        "@@USERNAME@@",
        "--ca-file",
        "@@CA_FILE@@"
      ],
      "cwd": "@@CLIENT_WORKSPACE@@",
      "directTools": [
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
        "get_logs"
      ]
    }
  }
}
```

Create `config/client/codex-mcp.example.toml`:

```toml
[mcp_servers.submitter]
command = "@@CLIENT_ENTRYPOINT@@"
args = [
  "--base-url",
  "@@MASTER_URL@@",
  "--username",
  "@@USERNAME@@",
  "--ca-file",
  "@@CA_FILE@@",
]
cwd = "@@CLIENT_WORKSPACE@@"
```

Create `config/client/dsh-mcp.example.patch.yml`:

```yaml
- insert:
    - id: mcp-submitter
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: submitter
        transport: stdio
        command: "@@CLIENT_ENTRYPOINT@@"
        args: ["--base-url", "@@MASTER_URL@@", "--username", "@@USERNAME@@", "--ca-file", "@@CA_FILE@@"]
        cwd: "@@CLIENT_WORKSPACE@@"
    - id: skill-filesystem-submitter
      name: '@deepseek-ai/dsh-skill-filesystem'
      config:
        providerName: submitter
        customSkillDirs:
          - "@@CLIENT_WORKSPACE@@/.agents/skills"
```

- [ ] **Step 5: Run onboarding and config checks**

Run:

```bash
python3 -m pytest tests/test_onboarding.py -v
python3 -m ruff check src/agent_scheduler/adapters/onboarding.py tests/test_onboarding.py
python3 -m mypy src/agent_scheduler/adapters/onboarding.py
```

Expected: all pass; `rg -n 'AGENT_SCHEDULER_STATE_ROOT|agent_scheduler\.cli\.main' config/client` prints no matches.

- [ ] **Step 6: Commit the source-free onboarding contract**

Run:

```bash
git status --short
git add config/client src/agent_scheduler/adapters/onboarding.py tests/test_onboarding.py
git commit -m "$(cat <<'EOF'
feat: render client-only onboarding configs

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Run qualification harnesses from an isolated client workspace

**Files:**
- Modify: `src/agent_scheduler/adapters/onboarding.py`
- Modify: `src/agent_scheduler/adapters/submitter.py`
- Modify: `src/agent_scheduler/qualification.py:57-171,543-552`
- Modify: `tests/test_submitter_harness.py`
- Modify: `tests/test_qualification.py:25-109`
- Modify: `tests/test_real_onboarding.py:32-63`
- Modify: `tests/test_real_qualification.py:74-82`

**Interfaces:**
- Consumes: source-free `build_onboarding()` from Task 4.
- Produces:
  - `prepare_client_workspace(project_root: Path, workspace: Path) -> Path`
  - `SubmitterInvocation(argv: tuple[str, ...], env: dict[str, str], prompt: str, cwd: Path)`
  - `build_submitter_invocation(harness: str, *, prompt_kind: Literal["qualification", "connectivity"] = "qualification", output_dir: Path, project_root: Path, client_workspace: Path, base_url: str, username: str, client_entrypoint: Path, ca_file: Path, executable: str | None = None, run_id: str) -> SubmitterInvocation`
  - optional `client_entrypoint: Path | None` parameter on `run_submitter_agent()`.

- [ ] **Step 1: Add failing workspace-isolation tests**

Update `_invoke()` in `tests/test_submitter_harness.py` to pass:

```python
client_workspace=tmp_path / "client-workspace",
client_entrypoint=Path("/opt/agent-client/venv/bin/agent-scheduler-submitter"),
ca_file=Path("/shared/state/tls/certificate.pem"),
```

Remove `state_root` and `python_path` arguments. Add:

```python
@pytest.mark.parametrize("harness", HARNESSES)
def test_invocation_contains_no_server_repository_path(harness: str, tmp_path: Path):
    invocation = _invoke(harness, tmp_path)
    rendered = "\n".join(
        [
            *invocation.argv,
            *[f"{key}={value}" for key, value in sorted(invocation.env.items())],
            invocation.prompt,
            str(invocation.cwd),
        ]
    )
    assert str(PROJECT_ROOT) not in rendered
    assert invocation.cwd == tmp_path / "client-workspace"
    assert (invocation.cwd / ".agents" / "skills" / "submit-gpu-task" / "SKILL.md").is_file()
    claude_skill = invocation.cwd / ".claude" / "skills" / "submit-gpu-task"
    assert claude_skill.is_symlink()
    assert claude_skill.resolve() == (
        invocation.cwd / ".agents" / "skills" / "submit-gpu-task"
    ).resolve()
```

Update `_stub_submitter_launch()` in `tests/test_qualification.py` so its `SubmitterInvocation` includes `cwd=tmp_path / "client-workspace"`, and assert subprocess receives that cwd.

- [ ] **Step 2: Run focused tests and verify signatures/cwd fail**

Run:

```bash
python3 -m pytest tests/test_submitter_harness.py tests/test_qualification.py -k 'workspace or invocation or zero_exit or timeout' -v
```

Expected: failures report unknown `client_workspace`/`client_entrypoint` arguments and missing `SubmitterInvocation.cwd`.

- [ ] **Step 3: Add deterministic client workspace preparation**

In `onboarding.py`, add:

```python
def prepare_client_workspace(project_root: Path, workspace: Path) -> Path:
    source = project_root / CANONICAL_SKILL_DIR
    if not (source / "SKILL.md").is_file():
        raise OnboardingError(f"canonical Submitter skill is missing: {source}")
    canonical = workspace / ".agents" / "skills" / "submit-gpu-task"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    if canonical.exists() or canonical.is_symlink():
        raise OnboardingError(f"client skill destination already exists: {canonical}")
    shutil.copytree(source, canonical)
    claude_root = workspace / ".claude" / "skills"
    claude_root.mkdir(parents=True, exist_ok=True)
    (claude_root / "submit-gpu-task").symlink_to(
        Path("../../.agents/skills/submit-gpu-task")
    )
    return canonical
```

Import `shutil`. Copy real files; do not copy the repository's `.claude` symlink.

- [ ] **Step 4: Extend invocation with cwd and public client command inputs**

Change the dataclass:

```python
@dataclass(frozen=True)
class SubmitterInvocation:
    argv: tuple[str, ...]
    env: dict[str, str]
    prompt: str
    cwd: Path
```

Change `build_submitter_invocation()` parameters by removing `state_root`, `python_path`, and `tls_certificate`, adding:

```python
client_workspace: Path,
client_entrypoint: Path,
ca_file: Path,
```

Pass `ca_file` to both `build_onboarding()` and `_base_environment()`; there is one public certificate path, not two aliases for it.

After validating the harness and, for pi, resolving the required provider/model arguments, call:

```python
prepare_client_workspace(project_root, client_workspace)
```

This ordering leaves no partial workspace when an invocation precondition is invalid. Then call `build_onboarding()` with `workspace=client_workspace`, `client_entrypoint`, and `ca_file`. Keep `project_root` only for reading trusted prompt source before launch.

For Codex, change `-C` to `str(client_workspace)`. Return `cwd=client_workspace` for every harness. The dsh prompt remains its final positional argument; Claude/Codex/pi continue receiving stdin as before.

- [ ] **Step 5: Launch and audit the isolated cwd in qualification**

Add to `run_submitter_agent()`:

```python
client_entrypoint: Path | None = None,
```

Resolve the default only when needed:

```python
resolved_client_entrypoint = client_entrypoint or Path(sys.executable).with_name(
    "agent-scheduler-submitter"
)
run_dir = state_root / "qualification" / run_id
client_workspace = run_dir / "workspace"
```

Pass both paths to `build_submitter_invocation()`. Change audit and process launch to:

```python
"cwd": str(invocation.cwd),
```

```python
cwd=invocation.cwd,
```

Update `_run_local_gates()` mypy command now, so qualification validates both source roots:

```python
[python_path, "-m", "mypy", "src", "packages/client/src"],
```

Update the gate assertion in `tests/test_qualification.py` accordingly.

- [ ] **Step 6: Update real T2/T3 callers without weakening opt-ins**

In `tests/test_real_onboarding.py`, derive:

```python
client_entrypoint = Path(sys.executable).with_name("agent-scheduler-submitter")
client_workspace = tmp_path / "client-workspace"
```

Pass those values and `ca_file=tls_certificate`; use `cwd=invocation.cwd` in `subprocess.run()`.

In `tests/test_real_qualification.py`, pass the same entrypoint to `run_submitter_agent()`. Do not change markers, credential checks, GPU checks, or timeouts.

- [ ] **Step 7: Run isolation, qualification, lint, and type checks**

Run:

```bash
python3 -m pytest tests/test_onboarding.py tests/test_submitter_harness.py \
  tests/test_qualification.py tests/test_real_onboarding.py \
  tests/test_real_qualification.py \
  -m 'not real_claude and not real_codex and not real_pi and not real_dsh and not real_gpu' -v
python3 -m ruff check src tests/test_submitter_harness.py tests/test_qualification.py
python3 -m mypy src packages/client/src
```

Expected: all non-real tests pass; real tests are collected and skipped, never invoked.

- [ ] **Step 8: Commit the source-free harness workspace**

Run:

```bash
git status --short
git add src/agent_scheduler/adapters/onboarding.py \
  src/agent_scheduler/adapters/submitter.py src/agent_scheduler/qualification.py \
  tests/test_submitter_harness.py tests/test_qualification.py \
  tests/test_real_onboarding.py tests/test_real_qualification.py
git commit -m "$(cat <<'EOF'
test: isolate submitter harnesses from server source

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Make the canonical skill client-safe and identity-generic

**Files:**
- Modify: `.agents/skills/submit-gpu-task/SKILL.md`
- Modify: `.agents/skills/submit-gpu-task/reference/proposal-template.md`
- Create: `tests/test_client_skill.py`

**Interfaces:**
- Consumes: dynamic configured username in `create_proposal` description from Task 2.
- Produces: one canonical cross-harness skill that never starts provider services and one template containing `<submitter-username>`.

- [ ] **Step 1: Write failing skill-boundary tests**

Create `tests/test_client_skill.py`:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / ".agents" / "skills" / "submit-gpu-task"


def test_client_skill_uses_semantic_tools_and_provider_fault_boundary():
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp__submitter__*" not in text
    assert "agent-scheduler serve" not in text
    assert "agent-scheduler worker" not in text
    assert "contact the service provider" in text
    for tool in ("create_proposal", "reply", "confirm_revision", "wait_for_task"):
        assert tool in text


def test_proposal_template_uses_configured_identity_placeholder():
    text = (SKILL_ROOT / "reference" / "proposal-template.md").read_text(
        encoding="utf-8"
    )
    assert "Submitter username: `<submitter-username>`." in text
    assert "Submitter username: `zz_chentian`." not in text
    assert "create_proposal" in text
    assert "configured submitter username" in text
```

- [ ] **Step 2: Run the tests and verify current provider instructions fail**

Run:

```bash
python3 -m pytest tests/test_client_skill.py -v
```

Expected: both tests fail on the current hard-coded prefix/service commands/username.

- [ ] **Step 3: Rewrite the skill's start boundary and tool naming**

Replace the opening rule in `SKILL.md` with:

```markdown
Drive one Proposal from creation to a terminal Task state using only the configured
`submitter` MCP tools. Harnesses may display prefixes differently; select tools by their
semantic names (`create_proposal`, `reply`, `confirm_revision`, and the read/wait tools).
REST is the authority; never edit Ground Truth files directly.
```

Replace “Before you start” with:

```markdown
## Before you start

Read the configured submitter username from the `create_proposal` tool description and use
that exact value in `## Identity`; never guess or reuse a historical username. If a tool call
cannot connect, stop, report the error, ask the client operator to check the issued endpoint,
CA, and MCP config, and contact the service provider. Do not start Master or Worker and do not
retry connectivity in a loop.
```

Keep the Proposal workflow, idempotency, frozen launcher, GPU constraints, terminal states, and honest-reporting rules unchanged.

- [ ] **Step 4: Generalize the Proposal template identity**

At the template introduction, add:

```markdown
Read `<submitter-username>` from the configured `create_proposal` tool description and replace
it exactly; do not submit the angle-bracket token literally.
```

Change the Identity line to:

```markdown
Submitter username: `<submitter-username>`.
```

Do not generalize Worker, container, image, launcher, mount, or artifact paths; those are required workload-contract facts.

- [ ] **Step 5: Run skill and onboarding tests**

Run:

```bash
python3 -m pytest tests/test_client_skill.py tests/test_onboarding.py -v
python3 -m ruff check tests/test_client_skill.py
```

Expected: all pass; `.claude/skills/submit-gpu-task` still resolves to the canonical skill.

- [ ] **Step 6: Commit the client-safe skill**

Run:

```bash
git status --short
git add .agents/skills/submit-gpu-task tests/test_client_skill.py
git commit -m "$(cat <<'EOF'
docs: make the submitter skill client-safe

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Write the standalone client guide and mark provider documentation

**Files:**
- Create: `docs/submitting-from-an-agent-client.md`
- Modify: `docs/submitting-from-an-agent-session.md:1-6`
- Modify: `README.md:16-22,49-52`
- Create: `tests/test_client_docs.py`
- Modify: `tests/test_onboarding.py:137-162`

**Interfaces:**
- Consumes: Kit layout, console entrypoint, skill paths, and config tokens established in Tasks 3–6.
- Produces: one standalone guide copied verbatim except `@@KIT_VERSION@@` rendering by the Kit builder.

- [ ] **Step 1: Write failing living-doc boundary tests**

Create `tests/test_client_docs.py`:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_DOC = PROJECT_ROOT / "docs" / "submitting-from-an-agent-client.md"

FORBIDDEN = (
    "/public/share/fh/agent-gpu-task-scheduler",
    "python3 -m agent_scheduler.cli.main",
    "build_onboarding",
    "AGENT_SCHEDULER_HARNESS_MODE",
    "ANTHROPIC_AUTH_TOKEN",
    "reload-users",
    "init-runtime",
    "ss -lntp",
    "curl -k",
    "curl -sk",
)

REQUIRED = (
    "python3 -m venv",
    "--no-index",
    "--find-links",
    "sha256sum -c SHA256SUMS",
    "submit-gpu-task",
    "--ca-file",
    "Claude Code",
    "Codex CLI",
    "pi",
    "dsh",
    "tools/list",
    "12",
    "联系服务方",
)


def test_client_doc_is_standalone_and_contains_only_client_actions():
    text = CLIENT_DOC.read_text(encoding="utf-8")
    for value in FORBIDDEN:
        assert value not in text
    for value in REQUIRED:
        assert value in text


def test_client_doc_uses_only_the_release_version_token():
    text = CLIENT_DOC.read_text(encoding="utf-8")
    tokens = {f"@@{value}@@" for value in text.split("@@")[1::2]}
    assert tokens == {"@@KIT_VERSION@@"}


def test_provider_doc_points_client_readers_to_the_client_doc():
    text = (PROJECT_ROOT / "docs" / "submitting-from-an-agent-session.md").read_text(
        encoding="utf-8"
    )
    assert "服务提供方内部" in text
    assert "submitting-from-an-agent-client.md" in text
```

Update the existing all-harness documentation test in `tests/test_onboarding.py` so it checks both documents, while keeping historical `docs/superpowers/` exclusions.

- [ ] **Step 2: Run doc tests and verify the client guide is missing**

Run:

```bash
python3 -m pytest tests/test_client_docs.py tests/test_onboarding.py -k 'doc or document' -v
```

Expected: `FileNotFoundError` for `docs/submitting-from-an-agent-client.md`.

- [ ] **Step 3: Write the client guide with this exact section contract**

Create `docs/submitting-from-an-agent-client.md` with these headings in order:

```markdown
# 从 Agent Client 提交 GPU Task
## 适用读者与边界
## 你应该已经收到什么
## 容器前置条件
## 步骤 1 · 校验 Client Kit
## 步骤 2 · 离线安装 client wheel
## 步骤 3 · 安装 submit-gpu-task skill
## 步骤 4 · 填写连接配置
## 步骤 5 · 选择一个 Agent harness
### Claude Code
### Codex CLI
### pi
### dsh
## 步骤 6 · 分层预检
## 步骤 7 · 触发任务
## Agent 会执行什么
## 客户端排障
## 安全边界
```

The installation section must use:

```bash
python3 -m venv /opt/agent-client/venv
/opt/agent-client/venv/bin/python3 -m pip install \
  --no-index \
  --find-links /opt/agent-client/kit/wheels \
  "agent-gpu-task-scheduler-client==@@KIT_VERSION@@"
```

The skill section must copy from the Kit into `<CLIENT_WORKSPACE>/.agents/skills` and create the same relative Claude symlink as `prepare_client_workspace()`.

The configuration section must export the four provider-issued values plus the deterministic entrypoint:

```bash
export MASTER_URL='https://master.example:8443'
export USERNAME='client_user-1'
export CA_FILE='/shared/agent-scheduler-mvp/tls/certificate.pem'
export CLIENT_WORKSPACE='/workspace'
export CLIENT_ENTRYPOINT='/opt/agent-client/venv/bin/agent-scheduler-submitter'
```

State that all example values must be replaced by values issued for the deployment. Include this renderer; using `"@" * 2` keeps the living document's only literal release token equal to `@@KIT_VERSION@@`:

```bash
python3 - "$SOURCE_TEMPLATE" "$RENDERED_CONFIG" <<'PY'
import os
import sys
from pathlib import Path

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
names = (
    "CLIENT_ENTRYPOINT",
    "MASTER_URL",
    "USERNAME",
    "CA_FILE",
    "CLIENT_WORKSPACE",
)
missing = [name for name in names if not os.environ.get(name)]
if missing:
    raise SystemExit(f"missing required values: {', '.join(missing)}")
marker = "@" * 2
text = source.read_text(encoding="utf-8")
for name in names:
    text = text.replace(f"{marker}{name}{marker}", os.environ[name])
if marker in text:
    raise SystemExit("rendered config still contains an unresolved token")
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(text, encoding="utf-8")
PY
```

For harness instructions:

- Claude Code uses `--strict-mcp-config --mcp-config <rendered-json>`.
- Codex uses three per-process `-c` overrides copied from the rendered TOML and does not call `codex mcp add`.
- pi states that `pi-mcp-adapter` must already be in the image or installed from an approved source, then uses the rendered shared JSON with `directTools`.
- dsh states the same external-plugin boundary and uses one `--patch` overlay containing both MCP and skill roots.

Include these concrete launch shapes:

```bash
claude --strict-mcp-config --mcp-config "$RENDERED_CONFIG"

codex \
  -c "mcp_servers.submitter.command=\"$CLIENT_ENTRYPOINT\"" \
  -c "mcp_servers.submitter.args=[\"--base-url\", \"$MASTER_URL\", \"--username\", \"$USERNAME\", \"--ca-file\", \"$CA_FILE\"]" \
  -c "mcp_servers.submitter.cwd=\"$CLIENT_WORKSPACE\""

pi --mcp-config "$RENDERED_CONFIG"

dsh --profile headless --patch "$RENDERED_CONFIG"
```

Clarify that the operator renders the JSON template for Claude/pi, the TOML template is a readable Codex reference while the command uses per-process overrides, and the YAML template is rendered for dsh.

The preflight must include, in order:

```bash
sha256sum -c SHA256SUMS
test -x "$CLIENT_ENTRYPOINT"
test -r "$CA_FILE"
curl --cacert "$CA_FILE" "$MASTER_URL/health"
```

Then show JSON-RPC `initialize` plus `tools/list` piped to the entrypoint, with the expected 12 semantic names. Explicitly explain that local `tools/list` does not touch REST.

The trigger example must include the issued username for defense in depth:

```text
使用配置中显示的 submitter username client_user-1，用 4 张卡提交一个 GPU 任务，并等待最终结果。
```

The troubleshooting table must route TLS hostname mismatch, connection refusal, timeout, and `403 USERNAME_NOT_ALLOWED` to “contact the service provider”; it must never provide server commands.

- [ ] **Step 4: Mark the old guide and README audiences**

Immediately below the title of `docs/submitting-from-an-agent-session.md`, add:

```markdown
> **读者范围：服务提供方内部部署与端到端联调。** 如果你只运行 Agent Client、没有服务端
> 源码访问权限，请使用 [纯客户端接入文档](submitting-from-an-agent-client.md)。
```

In `README.md`, list both:

```markdown
- [Agent Client 接入](docs/submitting-from-an-agent-client.md) — 不接触服务端源码，只安装 Client Kit、MCP 与 skill
- [服务方内部 Agent 联调](docs/submitting-from-an-agent-session.md) — 启动 Master/Worker 并验证四种 harness
```

Update later generic links to choose the correct audience rather than treating the internal guide as the only onboarding document.

- [ ] **Step 5: Run living-doc tests and formatting checks**

Run:

```bash
python3 -m pytest tests/test_client_docs.py tests/test_onboarding.py -v
python3 -m ruff check tests/test_client_docs.py tests/test_onboarding.py
git diff --check
```

Expected: all pass; the new guide contains no provider-only forbidden string.

- [ ] **Step 6: Commit the split-audience documentation**

Run:

```bash
git status --short
git add docs/submitting-from-an-agent-client.md \
  docs/submitting-from-an-agent-session.md README.md \
  tests/test_client_docs.py tests/test_onboarding.py
git commit -m "$(cat <<'EOF'
docs: add source-free Agent Client onboarding

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Build an allowlisted, hashed Client Kit with an offline smoke gate

**Files:**
- Create: `packages/client/wheelhouse-requirements.txt`
- Create: `src/agent_scheduler/client_kit.py`
- Create: `scripts/build_client_kit.py`
- Create: `tests/test_client_kit.py`

**Interfaces:**
- Consumes: built client wheel, dependency wheelhouse, canonical skill, three config templates, and client doc.
- Produces:

```python
@dataclass(frozen=True)
class KitBuildInputs:
    project_root: Path
    client_wheel: Path
    dependency_wheelhouse: Path
    output_dir: Path
    kit_version: str
    tested_harnesses: dict[str, str]


def build_client_kit(inputs: KitBuildInputs, *, smoke_install: bool = True) -> Path
```

- [ ] **Step 1: Write failing Kit allowlist, hash, and rejection tests**

Create `tests/test_client_kit.py` with deterministic zip-form wheel fixtures:

```python
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from agent_scheduler.client_kit import KitBuildInputs, build_client_kit

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_wheel(path: Path, distribution: str, package: str, version: str) -> Path:
    dist_info = f"{distribution.replace('-', '_')}-{version}.dist-info"
    metadata = (
        "Metadata-Version: 2.3\n"
        f"Name: {distribution}\n"
        f"Version: {version}\n"
        "Requires-Python: >=3.10\n"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{package}/__init__.py", "")
        archive.writestr(f"{dist_info}/METADATA", metadata)
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(f"{dist_info}/RECORD", "")
    return path


@pytest.fixture
def fake_client_wheel(tmp_path: Path) -> Path:
    return _write_wheel(
        tmp_path / "agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl",
        "agent-gpu-task-scheduler-client",
        "agent_scheduler_client",
        "0.2.0",
    )


@pytest.fixture
def fake_dependency_wheelhouse(tmp_path: Path) -> Path:
    wheelhouse = tmp_path / "dependency-wheels"
    wheelhouse.mkdir()
    _write_wheel(
        wheelhouse / "httpx-0.28.1-py3-none-any.whl",
        "httpx",
        "httpx",
        "0.28.1",
    )
    return wheelhouse


def test_builder_copies_only_allowlisted_client_artifacts(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr("agent_scheduler.client_kit._smoke_install", lambda *_args: None)
    output = build_client_kit(
        KitBuildInputs(
            project_root=PROJECT_ROOT,
            client_wheel=fake_client_wheel,
            dependency_wheelhouse=fake_dependency_wheelhouse,
            output_dir=tmp_path / "agent-client-kit-0.2.0",
            kit_version="0.2.0",
            tested_harnesses={
                "claude": "2.1.247",
                "codex": "0.149.1",
                "pi": "0.84.3",
                "dsh": "0.1.1-rc.2",
            },
        )
    )

    assert (output / "wheels" / fake_client_wheel.name).is_file()
    assert (output / "skills" / "submit-gpu-task" / "SKILL.md").is_file()
    assert (output / "config" / "mcp.example.json").is_file()
    assert (output / "docs" / "submitting-from-an-agent-client.md").is_file()
    assert not any(path.is_symlink() for path in output.rglob("*"))
    assert not (output / "src").exists()

    manifest = json.loads((output / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["kit_version"] == "0.2.0"
    assert manifest["client"]["distribution"] == "agent-gpu-task-scheduler-client"
    assert manifest["tool_count"] == 12
    assert set(manifest["tested_harnesses"]) == {"claude", "codex", "pi", "dsh"}

    checksum_lines = (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    covered = {line.split("  ", 1)[1] for line in checksum_lines}
    expected = {
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    assert covered == expected
    for line in checksum_lines:
        digest, relative = line.split("  ", 1)
        assert hashlib.sha256((output / relative).read_bytes()).hexdigest() == digest
```

Add rejection tests for:

- output already exists;
- client wheel contains `agent_scheduler/`;
- source skill contains a symlink;
- harness version map lacks any of the four names;
- client doc contains an unexpected `@@...@@` token after Kit version rendering;
- duplicate wheel filename has different bytes.

- [ ] **Step 2: Run tests and verify the builder module is missing**

Run:

```bash
python3 -m pytest tests/test_client_kit.py -v
```

Expected: collection fails because `agent_scheduler.client_kit` does not exist.

- [ ] **Step 3: Pin the release wheelhouse inputs**

Create `packages/client/wheelhouse-requirements.txt`:

```text
httpx==0.28.1
httpcore==1.0.9
anyio==4.14.2
certifi==2026.7.22
idna==3.19
h11==0.16.0
exceptiongroup==1.3.1
typing_extensions==4.16.0
```

The last two remain in every wheelhouse so one artifact supports Python 3.10–3.12 without relying on environment-marker resolution during download. Extra pure-Python wheels on newer Python are acceptable; missing Python 3.10 requirements are not.

- [ ] **Step 4: Implement the allowlisted Kit builder**

Create `src/agent_scheduler/client_kit.py` with:

```python
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path

from agent_scheduler_client.tools import SUBMITTER_TOOLS

_HARNESSES = {"claude", "codex", "pi", "dsh"}
_CONFIG_FILES = (
    "mcp.example.json",
    "codex-mcp.example.toml",
    "dsh-mcp.example.patch.yml",
)
_CONFIG_TOKENS = {
    "@@CLIENT_ENTRYPOINT@@",
    "@@MASTER_URL@@",
    "@@USERNAME@@",
    "@@CA_FILE@@",
    "@@CLIENT_WORKSPACE@@",
}
_TOKEN = re.compile(r"@@[A-Z_]+@@")


@dataclass(frozen=True)
class KitBuildInputs:
    project_root: Path
    client_wheel: Path
    dependency_wheelhouse: Path
    output_dir: Path
    kit_version: str
    tested_harnesses: dict[str, str]
```

Implement focused helpers with these signatures:

```python
def _validate_client_wheel(path: Path, version: str) -> None
def _copy_regular_tree(source: Path, destination: Path) -> None
def _copy_wheels(client_wheel: Path, wheelhouse: Path, destination: Path) -> None
def _render_client_doc(source: Path, destination: Path, version: str) -> None
def _validate_templates(config_dir: Path) -> None
def _write_manifest(root: Path, inputs: KitBuildInputs) -> None
def _write_sha256s(root: Path) -> None
def _smoke_install(root: Path, version: str) -> None
def build_client_kit(inputs: KitBuildInputs, *, smoke_install: bool = True) -> Path
```

Required behavior:

1. Reject an existing output path rather than merging or deleting it.
2. Validate the client wheel zip members before creating output. Reject absolute paths, `..`, symlinks, any `agent_scheduler/` member, or a missing `agent_scheduler_client/` member.
3. Create only `wheels/`, `skills/submit-gpu-task/`, `config/`, and `docs/` before manifest/hash files.
4. Copy every dependency `*.whl` and the client wheel. Same-name/same-bytes is idempotent; same-name/different-bytes raises `ValueError`.
5. Reject every source symlink or non-regular file in copied trees.
6. Copy the canonical skill from `.agents/skills/submit-gpu-task`.
7. Copy exactly `_CONFIG_FILES`; assert each contains exactly `_CONFIG_TOKENS` and no other `@@...@@` value.
8. Replace the single `@@KIT_VERSION@@` in the client doc and reject any remaining `@@...@@` value there.
9. Write the spec's manifest fields with sorted harness keys and `tool_count=len(SUBMITTER_TOOLS)`.
10. Write sorted SHA-256 lines in `<hex>  <posix-relative-path>` format for every regular file except `SHA256SUMS`.
11. Run `_smoke_install()` unless explicitly disabled by a unit test call.
12. On any failure after output creation, remove only the newly created output directory and re-raise. Never remove a path that existed before the call.

`_smoke_install()` creates a temporary venv with pip, then runs:

```python
[
    str(venv_python),
    "-m",
    "pip",
    "install",
    "--no-index",
    "--find-links",
    str(root / "wheels"),
    f"agent-gpu-task-scheduler-client=={version}",
]
```

It then invokes `agent-scheduler-submitter --help` and a Python assertion that `agent_scheduler_client` imports while `importlib.util.find_spec("agent_scheduler") is None`. Every subprocess call sets `check=True`, `capture_output=True`, and `text=True`; construct a child environment from `os.environ`, remove `PYTHONPATH`, and pass that environment explicitly.

- [ ] **Step 5: Add the thin command-line wrapper**

Create `scripts/build_client_kit.py` with argparse options:

```text
--project-root
--client-wheel
--dependency-wheelhouse
--output-dir
--kit-version
--harness-version NAME=VERSION   (repeat exactly four times)
```

Parse harness versions into a dictionary, reject duplicate names, call `build_client_kit()`, and print only the resulting output path. Do not add a command to the production scheduler CLI.

- [ ] **Step 6: Run Kit unit, lint, and type checks**

Run:

```bash
python3 -m pytest tests/test_client_kit.py -v
python3 -m ruff check src/agent_scheduler/client_kit.py scripts/build_client_kit.py \
  tests/test_client_kit.py
python3 -m mypy src/agent_scheduler/client_kit.py scripts/build_client_kit.py
```

Expected: all pass; tests monkeypatch only `_smoke_install`, not artifact validation/hash logic.

- [ ] **Step 7: Commit the reproducible Kit builder**

Run:

```bash
git status --short
git add packages/client/wheelhouse-requirements.txt \
  src/agent_scheduler/client_kit.py scripts/build_client_kit.py \
  tests/test_client_kit.py
git commit -m "$(cat <<'EOF'
feat: build a verified Agent Client Kit

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Prove the built wheel excludes server code and works from an empty workspace

**Files:**
- Modify: `tests/test_client_package.py`
- Create: `tests/test_client_isolation.py`
- Modify: `pyproject.toml` pytest markers only if a release-artifact marker is needed

**Interfaces:**
- Consumes: client package, CLI, and Kit builder from Tasks 1–8.
- Produces: release-artifact test entry via `AGENT_SCHEDULER_CLIENT_WHEEL`, plus an HTTPS fake-Master subprocess test that cannot import `agent_scheduler`.

- [ ] **Step 1: Add failing built-wheel member and metadata tests**

Extend `tests/test_client_package.py`:

```python
import os
import subprocess
import sys
import venv
import zipfile
from email.parser import Parser

import pytest


def _release_wheel() -> Path:
    configured = os.environ.get("AGENT_SCHEDULER_CLIENT_WHEEL")
    if not configured:
        pytest.skip("set AGENT_SCHEDULER_CLIENT_WHEEL to a built client wheel")
    path = Path(configured)
    assert path.is_file()
    return path


def test_built_client_wheel_contains_no_server_package():
    wheel = _release_wheel()
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert any(name.startswith("agent_scheduler_client/") for name in names)
        assert not any(name.startswith("agent_scheduler/") for name in names)
        assert not any("/prompts/" in name or "/tests/" in name for name in names)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
        text_members = [
            archive.read(name)
            for name in names
            if name.endswith((".py", ".txt", "METADATA", "entry_points.txt"))
        ]
    assert all(
        b"/public/share/fh/agent-gpu-task-scheduler" not in content
        for content in text_members
    )
    assert metadata["Name"] == "agent-gpu-task-scheduler-client"
    assert metadata["Version"] == "0.2.0"
    assert metadata["Requires-Python"] == ">=3.10"
    assert metadata.get_all("Requires-Dist") == ["httpx<1,>=0.27"]
```

Add this no-dependency install check; it proves metadata/entrypoint isolation without hiding a missing runtime wheelhouse:

```python
def test_built_client_wheel_installs_without_server_code(tmp_path: Path):
    wheel = _release_wheel()
    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python3"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
    )
    completed = subprocess.run(
        [str(venv_dir / "bin" / "agent-scheduler-submitter"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    assert "--ca-file" in completed.stdout
    isolated = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.util; import agent_scheduler_client; "
                "assert importlib.util.find_spec('agent_scheduler') is None"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    assert isolated.stderr == ""
```

This works because Task 3 keeps help parsing independent of `httpx` import.

- [ ] **Step 2: Build a wheel and run the artifact test to expose packaging mistakes**

Use a disposable build environment, never the project environment:

```bash
rm -rf /tmp/agent-client-build /tmp/agent-client-dist
python3 -m venv /tmp/agent-client-build
/tmp/agent-client-build/bin/python3 -m pip install 'build>=1,<2' 'hatchling>=1.25'
/tmp/agent-client-build/bin/python3 -m build --no-isolation --wheel \
  --outdir /tmp/agent-client-dist packages/client
AGENT_SCHEDULER_CLIENT_WHEEL="$(find /tmp/agent-client-dist -name '*.whl' -print -quit)" \
  python3 -m pytest tests/test_client_package.py -v

rm -rf /tmp/agent-server-dist
/tmp/agent-client-build/bin/python3 -m build --no-isolation --wheel \
  --outdir /tmp/agent-server-dist .
python3 - <<'PY'
from pathlib import Path
from zipfile import ZipFile
wheel = next(Path('/tmp/agent-server-dist').glob('*.whl'))
with ZipFile(wheel) as archive:
    names = archive.namelist()
assert any(name.startswith('agent_scheduler/') for name in names)
assert any(name.startswith('agent_scheduler_client/') for name in names)
print('server wheel bundles both private server and shared client packages')
PY
```

Expected before fixes: the tests identify any wrong package member, metadata, entrypoint, or import behavior. The root-wheel assertion also proves the compatibility wrapper remains installable. Do not weaken assertions; fix package metadata/source layout.

- [ ] **Step 3: Add a live fake-Master isolation test**

Create `tests/test_client_isolation.py`. Use this live HTTPS fixture shape so the client subprocess reaches a real fake-mode REST control plane rather than a mocked transport:

```python
import json
import os
import socket
import subprocess
import threading
import time
import venv
from pathlib import Path

import httpx
import pytest
import uvicorn
from conftest import proposal_markdown

from agent_scheduler.api.app import create_app
from agent_scheduler.config import Settings
from agent_scheduler.storage import EventStore


@pytest.fixture
def live_master(runtime_identity):
    root, identity = runtime_identity
    settings = Settings(
        state_root=root,
        harness_mode="fake",
        worker_mode="fake",
        qualification_profile=False,
        vram_threshold=2.0,
        allowed_users=frozenset({"zz_chentian"}),
        auto_schedule=False,
    )
    app = create_app(settings, identity)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            ssl_certfile=str(identity.tls_certificate),
            ssl_keyfile=str(identity.tls_private_key),
            log_level="error",
        )
    )
    thread = threading.Thread(
        target=lambda: server.run(sockets=[listener]),
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("test Master did not start")
    try:
        yield f"https://127.0.0.1:{port}", identity.tls_certificate, root
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()
```

The test itself must:

1. Read `AGENT_SCHEDULER_CLIENT_WHEEL` and skip only when no release wheel was supplied.
2. Create a temporary venv and install that wheel with `--no-deps`.
3. Set child `PYTHONPATH` to `Path(httpx.__file__).resolve().parents[1]`, the current environment's physical `site-packages` directory. Python does not process editable `.pth` files merely because the directory appears in `PYTHONPATH`; run a child assertion that `find_spec("agent_scheduler") is None` before MCP startup.
4. Create an empty temporary cwd containing no repository files.
5. Send three JSON-RPC lines: `initialize`, `tools/list`, and `tools/call` for `create_proposal`, using `proposal_markdown(1)` and idempotency key `isolated-client-create-1`.
6. Assert the child exits 0, stdout contains exactly three JSON objects, tools/list returns 12 tools, the decoded `tools/call.result.content[0].text` contains a new Proposal, stderr is empty, and `EventStore(root).read_snapshot("proposals", proposal_id)` is not `None`.

Use the exact child command:

```python
command = [
    str(venv_dir / "bin" / "agent-scheduler-submitter"),
    "--base-url",
    base_url,
    "--username",
    "zz_chentian",
    "--ca-file",
    str(certificate),
]
```

Ensure fixture shutdown sets `server.should_exit = True` and joins the thread with a finite timeout even when assertions fail.

- [ ] **Step 4: Run built-wheel isolation against the live fake Master**

Run:

```bash
AGENT_SCHEDULER_CLIENT_WHEEL="$(find /tmp/agent-client-dist -name '*.whl' -print -quit)" \
  python3 -m pytest tests/test_client_package.py tests/test_client_isolation.py -v
```

Expected: all pass; the child can create a Proposal while its Python environment cannot resolve `agent_scheduler`.

- [ ] **Step 5: Build a real dependency wheelhouse and dry-run the Kit builder**

Use the disposable build environment:

```bash
rm -rf /tmp/agent-client-wheelhouse /tmp/agent-client-kit-0.2.0
mkdir -p /tmp/agent-client-wheelhouse
/tmp/agent-client-build/bin/python3 -m pip download \
  --only-binary=:all: \
  --dest /tmp/agent-client-wheelhouse \
  --requirement packages/client/wheelhouse-requirements.txt
PYTHONPATH=src:packages/client/src python3 scripts/build_client_kit.py \
  --project-root . \
  --client-wheel "$(find /tmp/agent-client-dist -name '*.whl' -print -quit)" \
  --dependency-wheelhouse /tmp/agent-client-wheelhouse \
  --output-dir /tmp/agent-client-kit-0.2.0 \
  --kit-version 0.2.0 \
  --harness-version claude=2.1.247 \
  --harness-version codex=0.149.1 \
  --harness-version pi=0.84.3 \
  --harness-version dsh=0.1.1-rc.2
(cd /tmp/agent-client-kit-0.2.0 && sha256sum -c SHA256SUMS)
```

Expected: pip download is the only networked release-preparation step; Kit assembly and its smoke install use `--no-index` and pass.

- [ ] **Step 6: Run lint/type checks and commit isolation gates**

Run:

```bash
python3 -m ruff check tests/test_client_package.py tests/test_client_isolation.py
python3 -m mypy tests/test_client_package.py tests/test_client_isolation.py
git status --short
git add tests/test_client_package.py tests/test_client_isolation.py pyproject.toml
git commit -m "$(cat <<'EOF'
test: prove the client wheel excludes server code

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

Only stage `pyproject.toml` if this task adds a marker; do not create an empty metadata diff.

---

### Task 10: Align internal qualification docs and run every final gate

**Files:**
- Modify: `docs/testing-the-submitter.md`
- Modify: `README.md` if quality-gate commands still mention only `src`
- Modify: `docs/usage.md` only where it describes Submitter onboarding or mypy paths
- Modify: `tests/test_qualification.py` only if final gate evidence assertions need exact two-root commands
- Modify: any Task 1–9 file only to fix failures revealed by this task

**Interfaces:**
- Consumes: the complete implementation and release artifact from Tasks 1–9.
- Produces: accurate internal test instructions, a clean default gate, and explicit (not automatically executed) T2/T3 commands for all four harnesses.

- [ ] **Step 1: Add a failing internal-doc assertion for client-workspace testing**

Extend `tests/test_client_docs.py`:

```python
def test_internal_submitter_test_doc_describes_source_isolation():
    text = (PROJECT_ROOT / "docs" / "testing-the-submitter.md").read_text(
        encoding="utf-8"
    )
    assert "agent-scheduler-submitter" in text
    assert "client workspace" in text
    assert "不挂载服务端仓库" in text
    assert "python3 -m mypy src packages/client/src" in text
```

- [ ] **Step 2: Run the assertion and verify the internal guide is stale**

Run:

```bash
python3 -m pytest tests/test_client_docs.py -k internal_submitter -v
```

Expected: fail because `docs/testing-the-submitter.md` still describes source-root onboarding and `mypy src` only.

- [ ] **Step 3: Update internal testing and top-level quality-gate docs**

In `docs/testing-the-submitter.md`:

- Explain that T1 exercises `agent_scheduler_client` directly.
- Explain that T2/T3 create a per-run client workspace, copy only the canonical skill, launch `agent-scheduler-submitter`, and do not mount/use the service repository as Agent cwd.
- Keep provider-side Master/Worker prerequisites because this remains an internal testing guide.
- Change quality-gate commands to:

```bash
python3 -m pytest -m 'not real_claude and not real_codex and not real_pi and not real_dsh and not real_gpu'
python3 -m ruff check .
python3 -m mypy src packages/client/src
```

- Link client operators to `submitting-from-an-agent-client.md` and provider engineers to `submitting-from-an-agent-session.md`.
- Keep all real-test opt-in flags and cost warnings.

Update the same mypy command in `README.md` and `docs/usage.md`. Do not rewrite historical files under `docs/superpowers/`.

- [ ] **Step 4: Run the complete zero-cost gate with real opt-ins stripped**

Run:

```bash
env -u RUN_REAL_CLAUDE -u RUN_REAL_CODEX -u RUN_REAL_PI -u RUN_REAL_DSH \
    -u RUN_REAL_GPU -u RUN_FULL_QUALIFICATION \
  python3 -m pytest \
  -m 'not real_claude and not real_codex and not real_pi and not real_dsh and not real_gpu'
python3 -m ruff check .
python3 -m mypy src packages/client/src
git diff --check
```

Expected: all pass. If a failure occurs, fix the owning task's implementation; do not skip, xfail, broaden exception handling, or relax wheel/doc isolation assertions.

- [ ] **Step 5: Re-run the built artifact and Kit gates**

Run:

```bash
AGENT_SCHEDULER_CLIENT_WHEEL="$(find /tmp/agent-client-dist -name '*.whl' -print -quit)" \
  python3 -m pytest tests/test_client_package.py tests/test_client_isolation.py -v
(cd /tmp/agent-client-kit-0.2.0 && sha256sum -c SHA256SUMS)
python3 - <<'PY'
from pathlib import Path
root = Path('/tmp/agent-client-kit-0.2.0')
for path in root.rglob('*'):
    assert not path.is_symlink(), path
    assert 'agent_scheduler/' not in path.as_posix(), path
print('Client Kit source-isolation checks passed')
PY
```

Expected: all pass against freshly rebuilt Task 9 artifacts. If implementation changed after Task 9, rebuild the wheel and Kit before running these commands.

- [ ] **Step 6: Record but do not silently run the billed/GPU verification commands**

The following are the explicit post-merge or authorized-environment checks. Run each only when its environment and cost have been separately approved:

```bash
RUN_REAL_CLAUDE=1 python3 -m pytest tests/test_real_onboarding.py -m real_claude -v
RUN_REAL_CODEX=1  python3 -m pytest tests/test_real_onboarding.py -m real_codex -v
RUN_REAL_PI=1     python3 -m pytest tests/test_real_onboarding.py -m real_pi -v
RUN_REAL_DSH=1    python3 -m pytest tests/test_real_onboarding.py -m real_dsh -v
```

Then, one harness at a time:

```bash
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CLAUDE=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[claude-RUN_REAL_CLAUDE]' -v
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_CODEX=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[codex-RUN_REAL_CODEX]' -v
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_PI=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[pi-RUN_REAL_PI]' -v
RUN_REAL_GPU=1 RUN_FULL_QUALIFICATION=1 RUN_REAL_DSH=1 \
  python3 -m pytest \
  'tests/test_real_qualification.py::test_complete_real_qualification[dsh-RUN_REAL_DSH]' -v
```

If these are not run, report them as skipped authorization-dependent validation; do not imply they passed.

- [ ] **Step 7: Commit final documentation and gate fixes**

Run:

```bash
git status --short
git add README.md docs/testing-the-submitter.md docs/usage.md \
  tests/test_client_docs.py tests/test_qualification.py
# If a final gate required a source fix, add only that exact file after reviewing its diff.
git diff --cached --name-status
git commit -m "$(cat <<'EOF'
test: gate the complete Agent Client Kit

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

Do not stage directories or use `git add -A`; every final source fix must be named explicitly after `git diff -- <path>` review.
