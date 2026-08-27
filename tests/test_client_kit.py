from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agent_scheduler.client_kit import KitBuildInputs, build_client_kit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HARNESSES = {
    "claude": "2.1.247",
    "codex": "0.149.1",
    "pi": "0.84.3",
    "dsh": "0.1.1-rc.2",
}


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


def _write_smokeable_client_wheel(path: Path, version: str) -> Path:
    distribution = "agent-gpu-task-scheduler-client"
    dist_info = f"agent_gpu_task_scheduler_client-{version}.dist-info"
    members = {
        "agent_scheduler_client/__init__.py": "",
        "agent_scheduler_client/cli.py": "def main() -> int:\n    return 0\n",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.3\n"
            f"Name: {distribution}\n"
            f"Version: {version}\n"
            "Requires-Python: >=3.10\n"
        ),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (
            "[console_scripts]\n"
            "agent-scheduler-submitter = agent_scheduler_client.cli:main\n"
        ),
    }
    record = "\n".join([*(f"{name},," for name in members), f"{dist_info}/RECORD,,"])
    with zipfile.ZipFile(path, "w") as archive:
        for name, contents in members.items():
            archive.writestr(name, contents)
        archive.writestr(f"{dist_info}/RECORD", record)
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


@pytest.fixture
def public_project_root(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    skill_source = PROJECT_ROOT / ".agents" / "skills" / "submit-gpu-task"
    shutil.copytree(skill_source, root / ".agents" / "skills" / "submit-gpu-task")
    (root / "config" / "client").mkdir(parents=True)
    for name in (
        "mcp.example.json",
        "codex-mcp.example.toml",
        "dsh-mcp.example.patch.yml",
    ):
        shutil.copyfile(PROJECT_ROOT / "config" / "client" / name, root / "config" / "client" / name)
    (root / "docs").mkdir()
    shutil.copyfile(
        PROJECT_ROOT / "docs" / "submitting-from-an-agent-client.md",
        root / "docs" / "submitting-from-an-agent-client.md",
    )
    return root


def _inputs(
    *,
    project_root: Path,
    client_wheel: Path,
    dependency_wheelhouse: Path,
    output_dir: Path,
    tested_harnesses: dict[str, str] | None = None,
) -> KitBuildInputs:
    return KitBuildInputs(
        project_root=project_root,
        client_wheel=client_wheel,
        dependency_wheelhouse=dependency_wheelhouse,
        output_dir=output_dir,
        kit_version="0.2.0",
        tested_harnesses=dict(_HARNESSES if tested_harnesses is None else tested_harnesses),
    )


def test_builder_copies_only_allowlisted_client_artifacts(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("agent_scheduler.client_kit._smoke_install", lambda *_args: None)
    output = build_client_kit(
        _inputs(
            project_root=PROJECT_ROOT,
            client_wheel=fake_client_wheel,
            dependency_wheelhouse=fake_dependency_wheelhouse,
            output_dir=tmp_path / "agent-client-kit-0.2.0",
        )
    )

    assert (output / "wheels" / fake_client_wheel.name).is_file()
    assert (output / "skills" / "submit-gpu-task" / "SKILL.md").is_file()
    assert sorted(path.name for path in (output / "config").iterdir()) == [
        "codex-mcp.example.toml",
        "dsh-mcp.example.patch.yml",
        "mcp.example.json",
    ]
    rendered_doc = output / "docs" / "submitting-from-an-agent-client.md"
    assert rendered_doc.is_file()
    assert "agent-gpu-task-scheduler-client==0.2.0" in rendered_doc.read_text(encoding="utf-8")
    assert not any(path.is_symlink() for path in output.rglob("*"))
    assert not (output / "src").exists()

    manifest = json.loads((output / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest == {
        "kit_version": "0.2.0",
        "client": {
            "distribution": "agent-gpu-task-scheduler-client",
            "version": "0.2.0",
            "wheel": f"wheels/{fake_client_wheel.name}",
            "python_requires": ">=3.10",
        },
        "master_api": "v1",
        "mcp_server_name": "submitter",
        "tool_count": 12,
        "tested_harnesses": _HARNESSES,
    }
    assert list(manifest["tested_harnesses"]) == sorted(_HARNESSES)

    checksum_lines = (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert checksum_lines == sorted(checksum_lines, key=lambda line: line.split("  ", 1)[1])
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


def test_existing_output_is_rejected_without_modification(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            ),
            smoke_install=False,
        )

    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "member",
    [
        "/absolute.py",
        "../escape.py",
        "agent_scheduler_client/../../escape.py",
        "..\\escape.py",
        "C:\\escape.py",
    ],
)
def test_client_wheel_rejects_unsafe_member_paths(
    member: str,
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    with zipfile.ZipFile(fake_client_wheel, "a") as archive:
        archive.writestr(member, "")
    output = tmp_path / "kit"

    with pytest.raises(ValueError, match="unsafe wheel member"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            ),
            smoke_install=False,
        )

    assert not output.exists()


def test_client_wheel_rejects_symlink_member(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    link = zipfile.ZipInfo("agent_scheduler_client/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(fake_client_wheel, "a") as archive:
        archive.writestr(link, "target")

    with pytest.raises(ValueError, match="symlink"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
            ),
            smoke_install=False,
        )


def test_client_wheel_rejects_server_package(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    with zipfile.ZipFile(fake_client_wheel, "a") as archive:
        archive.writestr("agent_scheduler/__init__.py", "")

    with pytest.raises(ValueError, match="server package"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
            ),
            smoke_install=False,
        )


def test_client_wheel_requires_client_package(
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl",
        "agent-gpu-task-scheduler-client",
        "not_the_client",
        "0.2.0",
    )

    with pytest.raises(ValueError, match="agent_scheduler_client"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
            ),
            smoke_install=False,
        )


def test_client_wheel_version_must_equal_kit_version(
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    wheel = _write_wheel(
        tmp_path / "agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl",
        "agent-gpu-task-scheduler-client",
        "agent_scheduler_client",
        "0.1.0",
    )

    with pytest.raises(ValueError, match="wheel version"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
            ),
            smoke_install=False,
        )


def test_dependency_wheelhouse_rejects_server_package_wheel(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    _write_wheel(
        fake_dependency_wheelhouse / "agent_gpu_task_scheduler-0.2.0-py3-none-any.whl",
        "agent-gpu-task-scheduler",
        "agent_scheduler",
        "0.2.0",
    )

    with pytest.raises(ValueError, match="server package"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
            ),
            smoke_install=False,
        )


def test_dependency_wheelhouse_rejects_server_distribution_metadata(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    _write_wheel(
        fake_dependency_wheelhouse / "renamed-0.2.0-py3-none-any.whl",
        "agent-gpu-task-scheduler",
        "renamed_package",
        "0.2.0",
    )

    with pytest.raises(ValueError, match="server package"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
            ),
            smoke_install=False,
        )


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_source_skill_rejects_non_regular_entries(
    kind: str,
    public_project_root: Path,
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    special = public_project_root / ".agents" / "skills" / "submit-gpu-task" / "special"
    if kind == "symlink":
        special.symlink_to("SKILL.md")
    else:
        os.mkfifo(special)
    output = tmp_path / "kit"

    with pytest.raises(ValueError, match="regular"):
        build_client_kit(
            _inputs(
                project_root=public_project_root,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            ),
            smoke_install=False,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    "harnesses",
    [
        {"claude": "2.1.247", "codex": "0.149.1", "pi": "0.84.3"},
        {**_HARNESSES, "other": "1.0"},
    ],
)
def test_harness_version_map_requires_exact_allowlist(
    harnesses: dict[str, str],
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="harness"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=tmp_path / "kit",
                tested_harnesses=harnesses,
            ),
            smoke_install=False,
        )


@pytest.mark.parametrize("replacement", ["@@SURPRISE@@", "@@KIT_VERSION@@ @@KIT_VERSION@@"])
def test_client_doc_rejects_unexpected_or_duplicate_release_tokens(
    replacement: str,
    public_project_root: Path,
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    doc = public_project_root / "docs" / "submitting-from-an-agent-client.md"
    doc.write_text(
        doc.read_text(encoding="utf-8").replace("@@KIT_VERSION@@", replacement),
        encoding="utf-8",
    )
    output = tmp_path / "kit"

    with pytest.raises(ValueError, match="token"):
        build_client_kit(
            _inputs(
                project_root=public_project_root,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            ),
            smoke_install=False,
        )

    assert not output.exists()


@pytest.mark.parametrize("replacement", ["literal-username", "@@UNEXPECTED@@"])
def test_config_templates_require_exact_token_allowlist(
    replacement: str,
    public_project_root: Path,
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    template = public_project_root / "config" / "client" / "mcp.example.json"
    template.write_text(
        template.read_text(encoding="utf-8").replace("@@USERNAME@@", replacement),
        encoding="utf-8",
    )
    output = tmp_path / "kit"

    with pytest.raises(ValueError, match="template tokens"):
        build_client_kit(
            _inputs(
                project_root=public_project_root,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            ),
            smoke_install=False,
        )

    assert not output.exists()


def test_duplicate_wheel_filename_with_different_bytes_is_rejected(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    _write_wheel(
        fake_dependency_wheelhouse / fake_client_wheel.name,
        "different-distribution",
        "different_package",
        "0.2.0",
    )
    output = tmp_path / "kit"

    with pytest.raises(ValueError, match="duplicate wheel"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            ),
            smoke_install=False,
        )

    assert not output.exists()


def test_duplicate_wheel_filename_with_same_bytes_is_idempotent(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
) -> None:
    shutil.copyfile(fake_client_wheel, fake_dependency_wheelhouse / fake_client_wheel.name)

    output = build_client_kit(
        _inputs(
            project_root=PROJECT_ROOT,
            client_wheel=fake_client_wheel,
            dependency_wheelhouse=fake_dependency_wheelhouse,
            output_dir=tmp_path / "kit",
        ),
        smoke_install=False,
    )

    assert [path.name for path in (output / "wheels").glob(fake_client_wheel.name)] == [
        fake_client_wheel.name
    ]


def test_smoke_failure_removes_only_new_output(
    fake_client_wheel: Path,
    fake_dependency_wheelhouse: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "kit"

    def fail_smoke(*_args: object) -> None:
        raise RuntimeError("smoke failed")

    monkeypatch.setattr("agent_scheduler.client_kit._smoke_install", fail_smoke)

    with pytest.raises(RuntimeError, match="smoke failed"):
        build_client_kit(
            _inputs(
                project_root=PROJECT_ROOT,
                client_wheel=fake_client_wheel,
                dependency_wheelhouse=fake_dependency_wheelhouse,
                output_dir=output,
            )
        )

    assert not output.exists()


def _script_environment() -> dict[str, str]:
    environment = os.environ.copy()
    python_paths = [str(PROJECT_ROOT / "src"), str(PROJECT_ROOT / "packages" / "client" / "src")]
    if existing := environment.get("PYTHONPATH"):
        python_paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)
    return environment


def _script_command(
    *,
    client_wheel: Path,
    dependency_wheelhouse: Path,
    output_dir: Path,
) -> list[str]:
    return [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "build_client_kit.py"),
        "--project-root",
        str(PROJECT_ROOT),
        "--client-wheel",
        str(client_wheel),
        "--dependency-wheelhouse",
        str(dependency_wheelhouse),
        "--output-dir",
        str(output_dir),
        "--kit-version",
        "0.2.0",
    ]


def test_cli_builds_and_smoke_installs_kit_offline(tmp_path: Path) -> None:
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    client_wheel = _write_smokeable_client_wheel(
        tmp_path / "agent_gpu_task_scheduler_client-0.2.0-py3-none-any.whl",
        "0.2.0",
    )
    output = tmp_path / "kit"
    command = _script_command(
        client_wheel=client_wheel,
        dependency_wheelhouse=wheelhouse,
        output_dir=output,
    )
    for name, version in _HARNESSES.items():
        command.extend(["--harness-version", f"{name}={version}"])

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=_script_environment(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == f"{output}\n"
    assert completed.stderr == ""
    assert output.is_dir()


@pytest.mark.parametrize(
    ("versions", "message"),
    [
        (["claude=1", "codex=1", "pi=1"], "exactly four"),
        (["claude=1", "claude=2", "codex=1", "pi=1"], "duplicate harness name: claude"),
    ],
)
def test_cli_rejects_wrong_count_and_duplicate_harness_versions(
    versions: list[str],
    message: str,
    tmp_path: Path,
) -> None:
    command = _script_command(
        client_wheel=tmp_path / "client.whl",
        dependency_wheelhouse=tmp_path / "wheelhouse",
        output_dir=tmp_path / "kit",
    )
    for version in versions:
        command.extend(["--harness-version", version])

    completed = subprocess.run(
        command,
        cwd=tmp_path,
        env=_script_environment(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert message in completed.stderr
    assert completed.stdout == ""


def test_wheelhouse_requirements_pin_python_310_transitives() -> None:
    requirements = (PROJECT_ROOT / "packages" / "client" / "wheelhouse-requirements.txt")
    assert requirements.read_text(encoding="ascii").splitlines() == [
        "httpx==0.28.1",
        "httpcore==1.0.9",
        "anyio==4.14.2",
        "certifi==2026.7.22",
        "idna==3.19",
        "h11==0.16.0",
        "exceptiongroup==1.3.1",
        "typing_extensions==4.16.0",
    ]
