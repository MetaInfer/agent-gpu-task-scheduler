from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import threading
import time
import venv
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
import uvicorn

from agent_scheduler.api.app import create_app
from agent_scheduler.config import Settings
from agent_scheduler.runtime import RuntimeIdentity
from agent_scheduler.storage import EventStore

proposal_markdown = cast(
    Callable[[int], str],
    importlib.import_module("conftest").proposal_markdown,
)


@pytest.fixture
def live_master(
    runtime_identity: tuple[Path, RuntimeIdentity],
) -> Iterator[tuple[str, Path, Path]]:
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


def test_built_client_creates_proposal_from_empty_workspace(
    tmp_path: Path,
    live_master: tuple[str, Path, Path],
) -> None:
    configured = os.environ.get("AGENT_SCHEDULER_CLIENT_WHEEL")
    if not configured:
        pytest.skip("set AGENT_SCHEDULER_CLIENT_WHEEL to a built client wheel")
    wheel = Path(configured)
    assert wheel.is_file()

    venv_dir = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python3"
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    empty_workspace = tmp_path / "empty"
    empty_workspace.mkdir()
    dependency_site_packages = Path(httpx.__file__).resolve().parents[1]
    child_env = {
        **{key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        "PYTHONPATH": str(dependency_site_packages),
    }
    import_check = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.util; import agent_scheduler_client; import httpx; "
                "assert importlib.util.find_spec('agent_scheduler') is None"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=empty_workspace,
        env=child_env,
    )
    assert import_check.stdout == ""
    assert import_check.stderr == ""

    base_url, certificate, root = live_master
    command = [
        str(venv_dir / "bin" / "agent-scheduler-submitter"),
        "--base-url",
        base_url,
        "--username",
        "zz_chentian",
        "--ca-file",
        str(certificate),
    ]
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25"},
        },
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_proposal",
                "arguments": {
                    "markdown": proposal_markdown(1),
                    "idempotency_key": "isolated-client-create-1",
                },
            },
        },
    ]
    completed = subprocess.run(
        command,
        check=False,
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True,
        text=True,
        cwd=empty_workspace,
        env=child_env,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    lines = completed.stdout.splitlines()
    assert len(lines) == 3
    responses = [json.loads(line) for line in lines]
    assert all(isinstance(response, dict) for response in responses)
    assert [response["id"] for response in responses] == [1, 2, 3]
    assert len(responses[1]["result"]["tools"]) == 12

    result_text = responses[2]["result"]["content"][0]["text"]
    tool_result = json.loads(result_text)
    proposal = tool_result["proposal"]
    assert isinstance(proposal, dict)
    proposal_id = proposal["proposal_id"]
    assert isinstance(proposal_id, str)
    assert proposal_id.startswith("prop_")
    assert EventStore(root).read_snapshot("proposals", proposal_id) is not None
