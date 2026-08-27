import os
import subprocess
import venv
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath

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


def _inspect_client_wheel(wheel: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        assert any(name.startswith("agent_scheduler_client/") for name in names)
        assert not any(name.startswith("agent_scheduler/") for name in names)
        forbidden_components = {"prompts", "tests"}
        assert all(
            forbidden_components.isdisjoint(
                PurePosixPath(name.replace("\\", "/")).parts
            )
            for name in names
        )
        regular_contents = [archive.read(member) for member in members if not member.is_dir()]
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode("utf-8"))
    assert all(
        b"/public/share/fh/agent-gpu-task-scheduler" not in content
        for content in regular_contents
    )
    assert metadata["Name"] == "agent-gpu-task-scheduler-client"
    assert metadata["Version"] == "0.2.0"
    assert metadata["Requires-Python"] == ">=3.10"
    assert metadata.get_all("Requires-Dist") == ["httpx<1,>=0.27"]


def test_built_client_wheel_contains_no_server_package() -> None:
    _inspect_client_wheel(_release_wheel())


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
    empty_workspace = tmp_path / "empty"
    empty_workspace.mkdir()
    assert tuple(empty_workspace.iterdir()) == ()
    completed = subprocess.run(
        [str(venv_dir / "bin" / "agent-scheduler-submitter"), "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=empty_workspace,
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
        cwd=empty_workspace,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
    )
    assert isolated.stderr == ""
    assert tuple(empty_workspace.iterdir()) == ()


@pytest.mark.parametrize(
    ("bad_name", "bad_content"),
    [
        ("tests/leak.py", b"pass\n"),
        ("prompts/private.txt", b"private prompt\n"),
        (
            "agent_scheduler_client/data.bin",
            b"\x00/public/share/fh/agent-gpu-task-scheduler\x00",
        ),
    ],
    ids=("top-level-tests", "top-level-prompts", "binary-private-path"),
)
def test_built_client_wheel_rejects_forbidden_mutations(
    tmp_path: Path,
    bad_name: str,
    bad_content: bytes,
) -> None:
    source = _release_wheel()
    mutated = tmp_path / "mutated.whl"
    with zipfile.ZipFile(source) as source_archive, zipfile.ZipFile(mutated, "w") as output:
        for info in source_archive.infolist():
            output.writestr(info, source_archive.read(info))
        output.writestr(bad_name, bad_content)

    with pytest.raises(AssertionError):
        _inspect_client_wheel(mutated)
