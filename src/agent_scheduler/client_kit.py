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

from agent_scheduler_client import kit_verifier as kit_verifier_module
from agent_scheduler_client.kit_verifier import verify_client_kit
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


@dataclass(frozen=True)
class ClientKitRuntime:
    kit_root: Path
    virtual_environment: Path
    client_entrypoint: Path
    skill_source: Path
    config_source: Path


@dataclass(frozen=True)
class _DependencyWheel:
    distribution: str
    version: str
    source: Path

    def manifest_entry(self) -> dict[str, str]:
        return {
            "distribution": self.distribution,
            "version": self.version,
            "wheel": f"wheels/{self.source.name}",
        }


def _normalize_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


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


def _inspect_wheel(
    path: Path,
) -> tuple[zipfile.ZipFile, list[tuple[zipfile.ZipInfo, tuple[str, ...]]]]:
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
                distribution = _normalize_distribution(_metadata_value(metadata, "Name"))
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
    values = [
        line[len(prefix) :].strip() for line in metadata.splitlines() if line.startswith(prefix)
    ]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"client wheel must contain exactly one {field} metadata value")
    return values[0]


def _distribution_metadata(
    archive: zipfile.ZipFile,
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]],
) -> tuple[str, str]:
    metadata_members = [
        info
        for info, parts in members
        if len(parts) >= 2 and parts[-1] == "METADATA" and parts[-2].endswith(".dist-info")
    ]
    if len(metadata_members) != 1:
        raise ValueError("wheel must contain exactly one dist-info METADATA file")
    try:
        metadata = archive.read(metadata_members[0]).decode("utf-8")
    except (KeyError, UnicodeDecodeError) as error:
        raise ValueError("wheel contains invalid METADATA") from error
    return (
        _normalize_distribution(_metadata_value(metadata, "Name")),
        _metadata_value(metadata, "Version"),
    )


def _validate_client_wheel(path: Path, version: str) -> None:
    archive, members = _inspect_wheel(path)
    try:
        if not any(parts and parts[0] == _CLIENT_PACKAGE for _info, parts in members):
            raise ValueError(f"client wheel is missing the {_CLIENT_PACKAGE} package")
        distribution, wheel_version = _distribution_metadata(archive, members)
        if distribution != _normalize_distribution(_CLIENT_DISTRIBUTION):
            raise ValueError(f"client wheel distribution must be {_CLIENT_DISTRIBUTION}")
        if wheel_version != version:
            raise ValueError(
                f"client wheel version {wheel_version!r} does not match Kit version {version!r}"
            )
        metadata_info = next(
            info
            for info, parts in members
            if len(parts) >= 2 and parts[-1] == "METADATA" and parts[-2].endswith(".dist-info")
        )
        metadata = archive.read(metadata_info).decode("utf-8")
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


def _locked_dependencies(project_root: Path) -> list[tuple[str, str]]:
    path = project_root / "packages" / "client" / "wheelhouse-requirements.txt"
    _require_regular_file(path, "wheelhouse requirements")
    locked: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)", line)
        if match is None:
            raise ValueError(
                f"wheelhouse requirement line {line_number} must be an exact NAME==VERSION pin"
            )
        distribution = _normalize_distribution(match.group(1))
        version = match.group(2)
        if distribution in seen:
            raise ValueError(f"duplicate locked dependency distribution: {distribution}")
        if distribution == _normalize_distribution(_CLIENT_DISTRIBUTION):
            raise ValueError("client distribution must not appear in dependency requirements")
        seen.add(distribution)
        locked.append((distribution, version))
    if not locked:
        raise ValueError("wheelhouse requirements must contain at least one dependency")
    return locked


def _dependency_wheels(project_root: Path, wheelhouse: Path) -> list[_DependencyWheel]:
    _require_regular_directory(wheelhouse, "dependency wheelhouse")
    entries = sorted(wheelhouse.iterdir(), key=lambda path: path.name)
    for entry in entries:
        _require_regular_file(entry, "dependency wheelhouse entry")
        if entry.suffix != ".whl":
            raise ValueError(f"extra non-wheel file in dependency wheelhouse: {entry.name}")
    found: dict[str, _DependencyWheel] = {}
    for source in entries:
        archive, members = _inspect_wheel(source)
        try:
            if any(parts and parts[0] == _CLIENT_PACKAGE for _info, parts in members):
                raise ValueError(f"dependency wheel contains the client package: {source.name}")
            distribution, version = _distribution_metadata(archive, members)
        finally:
            archive.close()
        if distribution == _normalize_distribution(_CLIENT_DISTRIBUTION):
            raise ValueError(
                f"dependency wheel contains client distribution metadata: {source.name}"
            )
        if distribution in found:
            previous = found[distribution]
            raise ValueError(
                "duplicate dependency distribution in wheelhouse: "
                f"{distribution} ({previous.source.name}, {source.name})"
            )
        found[distribution] = _DependencyWheel(distribution, version, source)

    locked = _locked_dependencies(project_root)
    expected = {distribution for distribution, _version in locked}
    actual = set(found)
    if expected != actual:
        raise ValueError(
            "dependency wheelhouse set mismatch: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    ordered: list[_DependencyWheel] = []
    for distribution, version in locked:
        wheel = found[distribution]
        if wheel.version != version:
            raise ValueError(
                f"dependency wheel version mismatch for {distribution}: "
                f"expected {version}, received {wheel.version}"
            )
        ordered.append(wheel)
    return ordered


def _copy_wheels(
    project_root: Path,
    client_wheel: Path,
    wheelhouse: Path,
    destination: Path,
) -> list[dict[str, str]]:
    if (wheelhouse / client_wheel.name).exists() or (wheelhouse / client_wheel.name).is_symlink():
        raise ValueError(
            f"duplicate wheel filename between client and dependency wheelhouse: {client_wheel.name}"
        )
    dependencies = _dependency_wheels(project_root, wheelhouse)
    sources = [client_wheel, *(dependency.source for dependency in dependencies)]
    if len({source.name for source in sources}) != len(sources):
        raise ValueError("duplicate wheel filename between client and dependency wheelhouse")
    for source in sources:
        target = destination / source.name
        if target.exists() or target.is_symlink():
            raise ValueError(f"duplicate wheel destination: {source.name}")
        shutil.copyfile(source, target)
    return [dependency.manifest_entry() for dependency in dependencies]


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


def _write_manifest(
    root: Path,
    inputs: KitBuildInputs,
    dependencies: list[dict[str, str]],
) -> None:
    manifest = {
        "kit_version": inputs.kit_version,
        "client": {
            "distribution": _CLIENT_DISTRIBUTION,
            "version": inputs.kit_version,
            "wheel": f"wheels/{inputs.client_wheel.name}",
            "python_requires": ">=3.10",
        },
        "dependencies": dependencies,
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


def _manifest_install_paths(root: Path, manifest: dict[str, object]) -> list[Path]:
    client = manifest.get("client")
    dependencies = manifest.get("dependencies")
    if not isinstance(client, dict) or not isinstance(dependencies, list):
        raise TypeError("verified manifest is missing wheel declarations")
    client_wheel = client.get("wheel")
    if not isinstance(client_wheel, str):
        raise TypeError("verified manifest client wheel is invalid")
    paths: list[Path] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise TypeError("verified manifest dependency wheel is invalid")
        dependency_wheel = dependency.get("wheel")
        if not isinstance(dependency_wheel, str):
            raise TypeError("verified manifest dependency wheel is invalid")
        paths.append((root / dependency_wheel).resolve(strict=True))
    paths.append((root / client_wheel).resolve(strict=True))
    return paths


def _venv_commands(virtual_environment: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return (
            virtual_environment / "Scripts" / "python.exe",
            virtual_environment / "Scripts" / "agent-scheduler-submitter.exe",
        )
    return (
        virtual_environment / "bin" / "python",
        virtual_environment / "bin" / "agent-scheduler-submitter",
    )


def _offline_install(
    venv_python: Path,
    wheels: list[Path],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    subprocess.run(
        [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            *(str(wheel) for wheel in wheels),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
        cwd=cwd,
    )


def _smoke_install(root: Path, version: str) -> None:
    manifest = verify_client_kit(root)
    if manifest.get("kit_version") != version:
        raise ValueError("smoke version does not match verified Kit version")
    wheels = _manifest_install_paths(root, manifest)
    child_environment = os.environ.copy()
    child_environment.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="agent-client-kit-smoke-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        virtual_environment = temporary_root / "venv"
        workspace = temporary_root / "workspace"
        workspace.mkdir()
        venv.EnvBuilder(with_pip=True).create(virtual_environment)
        venv_python, submitter = _venv_commands(virtual_environment)
        _offline_install(
            venv_python,
            wheels,
            cwd=workspace,
            environment=child_environment,
        )
        for command in (
            [str(submitter), "--help"],
            [
                str(venv_python),
                "-c",
                (
                    "import importlib.util; import agent_scheduler_client; "
                    "assert importlib.util.find_spec('agent_scheduler') is None"
                ),
            ],
        ):
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=child_environment,
                cwd=workspace,
            )


def prepare_client_kit_runtime(kit_root: Path, runtime_root: Path) -> ClientKitRuntime:
    """Verify a Kit and install only its exact declared wheels into a fresh venv."""
    manifest = verify_client_kit(kit_root)
    if runtime_root.exists() or runtime_root.is_symlink():
        raise FileExistsError(f"Client Kit runtime already exists: {runtime_root}")
    runtime_root.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    try:
        venv.EnvBuilder(with_pip=True).create(runtime_root)
        venv_python, submitter = _venv_commands(runtime_root)
        _offline_install(
            venv_python,
            _manifest_install_paths(kit_root, manifest),
            cwd=runtime_root,
            environment=environment,
        )
        _require_regular_file(submitter, "installed client entrypoint")
    except BaseException:
        if runtime_root.exists() and runtime_root.is_dir() and not runtime_root.is_symlink():
            shutil.rmtree(runtime_root)
        raise
    return ClientKitRuntime(
        kit_root=kit_root,
        virtual_environment=runtime_root,
        client_entrypoint=submitter,
        skill_source=kit_root / "skills" / "submit-gpu-task",
        config_source=kit_root / "config",
    )


def _validate_harness_versions(tested_harnesses: dict[str, str]) -> None:
    names = set(tested_harnesses)
    if names != _HARNESSES:
        raise ValueError(
            f"tested harness names must be exactly {sorted(_HARNESSES)}; received {sorted(names)}"
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

        dependencies = _copy_wheels(
            inputs.project_root,
            inputs.client_wheel,
            inputs.dependency_wheelhouse,
            wheels,
        )
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
        verifier_file = kit_verifier_module.__file__
        if verifier_file is None:
            raise ValueError("client Kit verifier source is unavailable")
        verifier_source = Path(verifier_file)
        _require_regular_file(verifier_source, "client Kit verifier source")
        shutil.copyfile(verifier_source, output / "verify_client_kit.py")
        _write_manifest(output, inputs, dependencies)
        _write_sha256s(output)
        verify_client_kit(output)
        if smoke_install:
            _smoke_install(output, inputs.kit_version)
    except BaseException:
        shutil.rmtree(output)
        raise
    return output
