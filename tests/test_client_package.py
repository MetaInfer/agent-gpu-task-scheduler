import os
import subprocess
import venv
import zipfile
from email.parser import Parser
from pathlib import Path

import pytest
from agent_scheduler_client import __version__
from agent_scheduler_client.mcp import SubmitterMCPAdapter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _release_wheel() -> Path:
    configured = os.environ.get("AGENT_SCHEDULER_CLIENT_WHEEL")
    if not configured:
        pytest.skip("set AGENT_SCHEDULER_CLIENT_WHEEL to a built client wheel")
    path = Path(configured)
    assert path.is_file()
    return path


def test_client_adapter_has_one_source_location() -> None:
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


def test_built_client_wheel_contains_no_server_package() -> None:
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
        b"/public/share/fh/agent-gpu-task-scheduler" not in content for content in text_members
    )
    assert metadata["Name"] == "agent-gpu-task-scheduler-client"
    assert metadata["Version"] == "0.2.0"
    assert metadata["Requires-Python"] == ">=3.10"
    assert metadata.get_all("Requires-Dist") == ["httpx<1,>=0.27"]


def test_built_client_wheel_installs_without_server_code(tmp_path: Path) -> None:
    wheel = _release_wheel()
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
