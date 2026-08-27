"""Provider-side tooling for building a verified Agent Client Kit."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import venv
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

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
_TOKEN = re.compile(r"@@[^@\r\n]+@@")
_CLIENT_DISTRIBUTION = "agent-gpu-task-scheduler-client"
_CLIENT_PACKAGE = "agent_scheduler_client"
_SERVER_PACKAGE = "agent_scheduler"


@dataclass(frozen=True)
class KitBuildInputs:
    project_root: Path
    client_wheel: Path
    dependency_wheelhouse: Path
    output_dir: Path
    kit_version: str
    tested_harnesses: dict[str, str]


def _require_regular_file(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise ValueError(f"{description} is not a readable regular file: {path}") from error
    if not stat.S_ISREG(mode):
        raise ValueError(f"{description} must be a regular file: {path}")


def _require_regular_directory(path: Path, description: str) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise ValueError(f"{description} is not a readable directory: {path}") from error
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{description} must be a regular directory: {path}")


def _wheel_member_parts(name: str) -> tuple[str, ...]:
    windows_path = PureWindowsPath(name)
    normalized = PurePosixPath(name.replace("\\", "/"))
    if (
        not name
        or normalized.is_absolute()
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ".." in normalized.parts
    ):
        raise ValueError(f"unsafe wheel member path: {name!r}")
    return tuple(part for part in normalized.parts if part not in {"", "."})


def _inspect_wheel(path: Path) -> tuple[zipfile.ZipFile, list[tuple[zipfile.ZipInfo, tuple[str, ...]]]]:
    if path.suffix != ".whl":
        raise ValueError(f"wheel archive filename must end in .whl: {path}")
    _require_regular_file(path, "wheel")
    try:
        archive = zipfile.ZipFile(path)
        members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in archive.infolist():
            parts = _wheel_member_parts(info.filename)
            mode = info.external_attr >> 16
            file_type = stat.S_IFMT(mode)
            if stat.S_ISLNK(mode):
                raise ValueError(f"wheel contains a symlink member: {info.filename}")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError(
                    f"wheel member is not a regular file or directory: {info.filename}"
                )
            if parts and parts[0] == _SERVER_PACKAGE:
                raise ValueError(f"wheel contains the server package: {path}")
            members.append((info, parts))
        for info, parts in members:
            if len(parts) == 2 and parts[-1] == "METADATA" and parts[-2].endswith(".dist-info"):
                metadata = archive.read(info).decode("utf-8")
                distribution = re.sub(r"[-_.]+", "-", _metadata_value(metadata, "Name")).lower()
                if distribution == "agent-gpu-task-scheduler":
                    raise ValueError(f"wheel contains the server package distribution: {path}")
    except (OSError, zipfile.BadZipFile):
        raise ValueError(f"invalid wheel archive: {path}") from None
    except Exception:
        archive.close()
        raise
    return archive, members


def _metadata_value(metadata: str, field: str) -> str:
    prefix = f"{field}:"
    values = [line[len(prefix) :].strip() for line in metadata.splitlines() if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"client wheel must contain exactly one {field} metadata value")
    return values[0]


def _validate_client_wheel(path: Path, version: str) -> None:
    archive, members = _inspect_wheel(path)
    try:
        if not any(parts and parts[0] == _CLIENT_PACKAGE for _info, parts in members):
            raise ValueError(f"client wheel is missing the {_CLIENT_PACKAGE} package")
        metadata_members = [
            info
            for info, parts in members
            if len(parts) >= 2
            and parts[-1] == "METADATA"
            and parts[-2].endswith(".dist-info")
        ]
        if len(metadata_members) != 1:
            raise ValueError("client wheel must contain exactly one dist-info METADATA file")
        try:
            metadata = archive.read(metadata_members[0]).decode("utf-8")
        except (KeyError, UnicodeDecodeError) as error:
            raise ValueError("client wheel contains invalid METADATA") from error
        if _metadata_value(metadata, "Name") != _CLIENT_DISTRIBUTION:
            raise ValueError(f"client wheel distribution must be {_CLIENT_DISTRIBUTION}")
        wheel_version = _metadata_value(metadata, "Version")
        if wheel_version != version:
            raise ValueError(f"client wheel version {wheel_version!r} does not match Kit version {version!r}")
        if _metadata_value(metadata, "Requires-Python") != ">=3.10":
            raise ValueError("client wheel Requires-Python must be >=3.10")
    finally:
        archive.close()


def _copy_regular_tree(source: Path, destination: Path) -> None:
    _require_regular_directory(source, "tree source")
    entries = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    for entry in entries:
        mode = entry.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"tree source entries must be regular files or directories: {entry}")

    destination.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        relative = entry.relative_to(source)
        target = destination / relative
        if stat.S_ISDIR(entry.lstat().st_mode):
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(entry, target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_wheels(client_wheel: Path, wheelhouse: Path, destination: Path) -> None:
    _require_regular_directory(wheelhouse, "dependency wheelhouse")
    dependency_wheels = sorted(wheelhouse.glob("*.whl"), key=lambda path: path.name)
    sources = [client_wheel, *dependency_wheels]
    for source in sources:
        archive, _members = _inspect_wheel(source)
        archive.close()

    for source in sources:
        target = destination / source.name
        if target.exists() or target.is_symlink():
            _require_regular_file(target, "copied wheel")
            if _sha256(source) == _sha256(target):
                continue
            raise ValueError(f"duplicate wheel filename has different bytes: {source.name}")
        shutil.copyfile(source, target)


def _render_client_doc(source: Path, destination: Path, version: str) -> None:
    _require_regular_file(source, "client documentation")
    text = source.read_text(encoding="utf-8")
    if text.count("@@KIT_VERSION@@") != 1:
        raise ValueError("client documentation must contain exactly one @@KIT_VERSION@@ token")
    rendered = text.replace("@@KIT_VERSION@@", version)
    remaining = sorted(set(_TOKEN.findall(rendered)))
    if remaining:
        raise ValueError(f"client documentation contains unexpected release tokens: {remaining}")
    destination.write_text(rendered, encoding="utf-8")


def _validate_templates(config_dir: Path) -> None:
    for name in _CONFIG_FILES:
        path = config_dir / name
        _require_regular_file(path, "configuration template")
        tokens = set(_TOKEN.findall(path.read_text(encoding="utf-8")))
        if tokens != _CONFIG_TOKENS:
            raise ValueError(
                f"configuration template tokens do not match the allowlist for {name}: {sorted(tokens)}"
            )


def _write_manifest(root: Path, inputs: KitBuildInputs) -> None:
    manifest = {
        "kit_version": inputs.kit_version,
        "client": {
            "distribution": _CLIENT_DISTRIBUTION,
            "version": inputs.kit_version,
            "wheel": f"wheels/{inputs.client_wheel.name}",
            "python_requires": ">=3.10",
        },
        "master_api": "v1",
        "mcp_server_name": "submitter",
        "tool_count": len(SUBMITTER_TOOLS),
        "tested_harnesses": dict(sorted(inputs.tested_harnesses.items())),
    }
    (root / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_sha256s(root: Path) -> None:
    files: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Kit entries must be regular files or directories: {relative}")
        if relative != "SHA256SUMS":
            files.append((relative, path))
    lines = [f"{_sha256(path)}  {relative}" for relative, path in sorted(files)]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")


def _smoke_install(root: Path, version: str) -> None:
    wheelhouse = (root / "wheels").resolve(strict=True)
    child_environment = os.environ.copy()
    child_environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="agent-client-kit-smoke-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        virtual_environment = temporary_root / "venv"
        workspace = temporary_root / "workspace"
        workspace.mkdir()
        venv.EnvBuilder(with_pip=True).create(virtual_environment)
        if os.name == "nt":
            venv_python = virtual_environment / "Scripts" / "python.exe"
            submitter = virtual_environment / "Scripts" / "agent-scheduler-submitter.exe"
        else:
            venv_python = virtual_environment / "bin" / "python"
            submitter = virtual_environment / "bin" / "agent-scheduler-submitter"

        commands = (
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                f"{_CLIENT_DISTRIBUTION}=={version}",
            ],
            [str(submitter), "--help"],
            [
                str(venv_python),
                "-c",
                (
                    "import importlib.util; import agent_scheduler_client; "
                    "assert importlib.util.find_spec('agent_scheduler') is None"
                ),
            ],
        )
        for command in commands:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=child_environment,
                cwd=workspace,
            )


def _validate_harness_versions(tested_harnesses: dict[str, str]) -> None:
    names = set(tested_harnesses)
    if names != _HARNESSES:
        raise ValueError(
            "tested harness names must be exactly "
            f"{sorted(_HARNESSES)}; received {sorted(names)}"
        )
    if any(not version for version in tested_harnesses.values()):
        raise ValueError("tested harness versions must not be empty")


def build_client_kit(inputs: KitBuildInputs, *, smoke_install: bool = True) -> Path:
    output = inputs.output_dir
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Client Kit output already exists: {output}")
    _validate_harness_versions(inputs.tested_harnesses)
    _validate_client_wheel(inputs.client_wheel, inputs.kit_version)

    output.mkdir()
    try:
        wheels = output / "wheels"
        skill = output / "skills" / "submit-gpu-task"
        config = output / "config"
        docs = output / "docs"
        wheels.mkdir()
        skill.mkdir(parents=True)
        config.mkdir()
        docs.mkdir()

        _copy_wheels(inputs.client_wheel, inputs.dependency_wheelhouse, wheels)
        _copy_regular_tree(
            inputs.project_root / ".agents" / "skills" / "submit-gpu-task",
            skill,
        )
        source_config = inputs.project_root / "config" / "client"
        for name in _CONFIG_FILES:
            source = source_config / name
            _require_regular_file(source, "configuration template")
            shutil.copyfile(source, config / name)
        _validate_templates(config)
        _render_client_doc(
            inputs.project_root / "docs" / "submitting-from-an-agent-client.md",
            docs / "submitting-from-an-agent-client.md",
            inputs.kit_version,
        )
        _write_manifest(output, inputs)
        _write_sha256s(output)
        if smoke_install:
            _smoke_install(output, inputs.kit_version)
    except BaseException:
        shutil.rmtree(output)
        raise
    return output
