import io
import os
import signal
import subprocess
import sys
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


@pytest.mark.parametrize("kind", ["missing", "directory", "unreadable"])
def test_cli_rejects_missing_nonregular_or_unreadable_ca(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_file = tmp_path / "certificate.pem"
    if kind == "directory":
        ca_file.mkdir()
    elif kind == "unreadable":
        ca_file.write_text("certificate", encoding="ascii")
        real_access = os.access
        monkeypatch.setattr(
            cli.os,
            "access",
            lambda path, mode: False if Path(path) == ca_file else real_access(path, mode),
        )

    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "--base-url",
                "https://master.example",
                "--username",
                "client_user-1",
                "--ca-file",
                str(ca_file),
            ]
        )


def test_run_mcp_closes_adapter_when_stdio_raises_system_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_file = tmp_path / "certificate.pem"
    ca_file.write_text("certificate", encoding="ascii")
    closed = False

    class ExitingAdapter:
        def __init__(self, _base_url: str, _username: str, verify: str) -> None:
            assert verify == str(ca_file)

        def run_stdio(self, _input_stream: object, _output_stream: object) -> None:
            raise SystemExit(143)

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(cli, "_adapter_type", lambda: ExitingAdapter)

    with pytest.raises(SystemExit, match="143"):
        cli.run_mcp(
            base_url="https://master.example",
            username="client_user-1",
            ca_file=ca_file,
            input_stream=io.StringIO(),
            output_stream=io.StringIO(),
        )
    assert closed


def test_main_installs_sigterm_cleanup_handler_and_restores_previous_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca_file = tmp_path / "certificate.pem"
    ca_file.write_text("certificate", encoding="ascii")
    previous = object()
    installed: list[object] = []

    def fake_signal(signum: int, handler: object) -> object:
        assert signum == signal.SIGTERM
        installed.append(handler)
        return previous

    def fake_run_mcp(**_kwargs: object) -> int:
        handler = installed[-1]
        assert callable(handler)
        handler(signal.SIGTERM, None)
        return 0

    monkeypatch.setattr(cli.signal, "signal", fake_signal)
    monkeypatch.setattr(cli, "run_mcp", fake_run_mcp)

    with pytest.raises(SystemExit, match="143"):
        cli.main(
            [
                "--base-url",
                "https://master.example",
                "--username",
                "client_user-1",
                "--ca-file",
                str(ca_file),
            ]
        )
    assert installed == [cli._terminate, previous]


def test_help_is_dependency_isolated() -> None:
    project_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project_root / "packages" / "client" / "src")
    completed = subprocess.run(
        [sys.executable, "-S", "-m", "agent_scheduler_client", "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=project_root,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--ca-file" in completed.stdout
    assert completed.stderr == ""
