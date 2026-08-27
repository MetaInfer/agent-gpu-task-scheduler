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
