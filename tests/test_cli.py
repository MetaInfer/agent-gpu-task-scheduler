import subprocess
import sys
from pathlib import Path

from agent_scheduler.cli import main as cli_main
from agent_scheduler.runtime import init_runtime


def test_mcp_command_never_calls_load_runtime(tmp_path: Path, monkeypatch):
    """The `mcp` CLI command backs the Submitter, which is deliberately unprivileged per
    the spec's trust model. Deleting every other secret and asserting on `load_runtime`
    itself proves the command reaches the TLS certificate without touching any of them."""
    root = tmp_path / "state"
    init_runtime(root)
    for name in (
        "ed25519-private.pem",
        "ed25519-public.pem",
        "ed25519-key-id",
        "worker-api-key",
    ):
        (root / "secrets" / name).unlink()

    def _forbidden(*_args, **_kwargs):
        raise AssertionError("mcp command must not call load_runtime")

    monkeypatch.setattr(cli_main, "load_runtime", _forbidden)
    monkeypatch.setenv("AGENT_SCHEDULER_STATE_ROOT", str(root))

    started = {}

    def fake_run_mcp(*, base_url, username, ca_file):
        started.update(
            base_url=base_url,
            username=username,
            ca_file=ca_file,
        )
        return 0

    monkeypatch.setattr(cli_main, "run_mcp", fake_run_mcp)

    exit_code = cli_main.main(
        ["mcp", "--base-url", "https://127.0.0.1:8443", "--username", "zz_chentian"]
    )
    assert exit_code == 0
    assert started == {
        "base_url": "https://127.0.0.1:8443",
        "username": "zz_chentian",
        "ca_file": root / "tls" / "certificate.pem",
    }


def test_python_module_entrypoint_reaches_cli_help():
    completed = subprocess.run(
        [sys.executable, "-m", "agent_scheduler.cli.main", "--help"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert "agent-scheduler" in completed.stdout
    assert "qualify" in completed.stdout


def test_qualify_accepts_a_harness_flag():
    from agent_scheduler.cli.main import build_parser

    args = build_parser().parse_args(["qualify", "--harness", "dsh"])
    assert args.harness == "dsh"


def test_qualify_defaults_to_claude():
    from agent_scheduler.cli.main import build_parser

    assert build_parser().parse_args(["qualify"]).harness == "claude"


def test_qualify_rejects_an_unknown_harness():
    import pytest

    from agent_scheduler.cli.main import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["qualify", "--harness", "gemini"])
