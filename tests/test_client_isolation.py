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
from contextlib import contextmanager
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
def release_wheel() -> Path:
    configured = os.environ.get("AGENT_SCHEDULER_CLIENT_WHEEL")
    if not configured:
        pytest.skip("set AGENT_SCHEDULER_CLIENT_WHEEL to a built client wheel")
    wheel = Path(configured)
    assert wheel.is_file()
    return wheel


@contextmanager
def _serve_live_master(
    root: Path,
    identity: RuntimeIdentity,
) -> Iterator[tuple[str, Path, Path]]:
    listener: socket.socket | None = None
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    try:
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
        running_listener = listener
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
        running_server = server
        thread = threading.Thread(
            target=lambda: running_server.run(sockets=[running_listener]),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            raise RuntimeError("test Master did not start")
        yield f"https://127.0.0.1:{port}", identity.tls_certificate, root
    finally:
        try:
            if server is not None:
                server.should_exit = True
            if thread is not None:
                thread.join(timeout=10)
        finally:
            if listener is not None:
                listener.close()
        if thread is not None:
            assert not thread.is_alive(), "test Master thread did not stop"


@pytest.fixture
def live_master(
    release_wheel: Path,
    runtime_identity: tuple[Path, RuntimeIdentity],
) -> Iterator[tuple[str, Path, Path]]:
    assert release_wheel.is_file()
    root, identity = runtime_identity
    with _serve_live_master(root, identity) as master:
        yield master


def test_built_client_creates_proposal_from_empty_workspace(
    tmp_path: Path,
    release_wheel: Path,
    live_master: tuple[str, Path, Path],
) -> None:
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
            str(release_wheel),
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


@pytest.mark.parametrize(
    ("thread_stays_alive", "expected_error"),
    [
        (False, "test Master did not start"),
        (True, "test Master thread did not stop"),
    ],
    ids=("cleanup-succeeds", "cleanup-fails"),
)
def test_live_master_startup_failure_always_cleans_up(
    runtime_identity: tuple[Path, RuntimeIdentity],
    monkeypatch: pytest.MonkeyPatch,
    thread_stays_alive: bool,
    expected_error: str,
) -> None:
    class FakeListener:
        def __init__(self) -> None:
            self.closed = False

        def bind(self, address: tuple[str, int]) -> None:
            assert address == ("127.0.0.1", 0)

        def listen(self) -> None:
            pass

        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", 43210

        def close(self) -> None:
            self.closed = True

    class FakeServer:
        def __init__(self) -> None:
            self.started = False
            self.should_exit = False

    class FakeThread:
        def __init__(self) -> None:
            self.join_timeouts: list[float | None] = []

        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return bool(self.join_timeouts) and thread_stays_alive

        def join(self, timeout: float | None = None) -> None:
            self.join_timeouts.append(timeout)

    listener = FakeListener()
    server = FakeServer()
    thread = FakeThread()
    monkeypatch.setattr(socket, "socket", lambda: listener)
    monkeypatch.setattr(uvicorn, "Server", lambda _config: server)
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *, target, daemon: thread,
    )

    root, identity = runtime_identity
    expected_type: type[BaseException] = AssertionError if thread_stays_alive else RuntimeError
    with (
        pytest.raises(expected_type, match=expected_error),
        _serve_live_master(root, identity),
    ):
        pytest.fail("startup failure must not yield")

    assert server.should_exit is True
    assert thread.join_timeouts == [10]
    assert listener.closed is True
    assert thread.is_alive() is thread_stays_alive
