"""Stdlib-only verification for an unpacked Agent Client Kit."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath


def _safe_relative(value: object, description: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"{description} must be a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError(f"{description} is unsafe: {value!r}")
    return path.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(root: Path) -> set[str]:
    try:
        root_mode = root.lstat().st_mode
    except OSError as exc:
        raise ValueError(f"Client Kit root is missing or unreadable: {root}") from exc
    if not stat.S_ISDIR(root_mode):
        raise ValueError(f"Client Kit root must be a regular directory: {root}")
    files: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        try:
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ValueError(f"Client Kit entry is unreadable: {relative}") from exc
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(
                f"Client Kit entries must be regular files or directories; symlink or special entry: {relative}"
            )
        files.add(relative)
    return files


def _read_hashes(root: Path) -> dict[str, str]:
    checksum_path = root / "SHA256SUMS"
    try:
        lines = checksum_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ValueError("SHA256SUMS is missing or unreadable") from exc
    hashes: dict[str, str] = {}
    for line in lines:
        digest, separator, raw_relative = line.partition("  ")
        if (
            not separator
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError(f"malformed SHA256SUMS line: {line!r}")
        relative = _safe_relative(raw_relative, "checksum path")
        if relative == "SHA256SUMS" or relative in hashes:
            raise ValueError(f"duplicate or recursive checksum path: {relative}")
        hashes[relative] = digest
    if not hashes:
        raise ValueError("SHA256SUMS must contain at least one file")
    return hashes


def _read_manifest(root: Path) -> dict[str, object]:
    try:
        value = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MANIFEST.json is missing or invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("MANIFEST.json must contain a JSON object")  # noqa: TRY004
    return value


def _normalized_distribution(value: str) -> str:
    normalized = value.lower()
    for separator in ("_", "."):
        normalized = normalized.replace(separator, "-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized


def _wheel_identity(root: Path, relative: str) -> tuple[str, str]:
    try:
        with zipfile.ZipFile(root / relative) as archive:
            metadata_members = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_members) != 1:
                raise ValueError(f"wheel must contain exactly one dist-info METADATA: {relative}")
            metadata = Parser().parsestr(archive.read(metadata_members[0]).decode("utf-8"))
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ValueError(f"manifest wheel is invalid: {relative}") from exc
    distribution = metadata.get("Name")
    version = metadata.get("Version")
    if not distribution or not version:
        raise ValueError(f"wheel metadata is missing Name or Version: {relative}")
    return _normalized_distribution(distribution), version


def _manifest_wheels(root: Path, manifest: dict[str, object]) -> set[str]:
    expected_keys = {
        "kit_version",
        "client",
        "dependencies",
        "master_api",
        "mcp_server_name",
        "tool_count",
        "tested_harnesses",
    }
    if set(manifest) != expected_keys:
        raise ValueError(
            "manifest keys do not match the schema: "
            f"missing={sorted(expected_keys - set(manifest))}, "
            f"extra={sorted(set(manifest) - expected_keys)}"
        )
    kit_version = manifest.get("kit_version")
    client = manifest.get("client")
    dependencies = manifest.get("dependencies")
    if not isinstance(kit_version, str) or not kit_version:
        raise ValueError("manifest kit_version must be a non-empty string")
    if not isinstance(client, dict):
        raise ValueError("manifest client must be an object")  # noqa: TRY004
    if set(client) != {"distribution", "version", "wheel", "python_requires"}:
        raise ValueError("manifest client keys do not match the schema")
    if client.get("distribution") != "agent-gpu-task-scheduler-client":
        raise ValueError("manifest client distribution is invalid")
    if client.get("python_requires") != ">=3.10":
        raise ValueError("manifest client python_requires is invalid")
    if client.get("version") != kit_version:
        raise ValueError("manifest client version must equal kit_version")
    client_wheel = _safe_relative(client.get("wheel"), "manifest client wheel")
    client_identity = _wheel_identity(root, client_wheel)
    if client_identity != (
        "agent-gpu-task-scheduler-client",
        kit_version,
    ):
        raise ValueError("manifest client distribution/version does not match wheel metadata")
    if not isinstance(dependencies, list):
        raise ValueError("manifest dependencies must be an array")  # noqa: TRY004
    if not dependencies:
        raise ValueError("manifest dependencies must not be empty")
    if (
        manifest.get("master_api") != "v1"
        or manifest.get("mcp_server_name") != "submitter"
        or manifest.get("tool_count") != 12
    ):
        raise ValueError("manifest API, MCP server, or tool contract is invalid")
    tested_harnesses = manifest.get("tested_harnesses")
    if not isinstance(tested_harnesses, dict):
        raise ValueError("manifest tested_harnesses must be an object")  # noqa: TRY004
    if set(tested_harnesses) != {"claude", "codex", "pi", "dsh"} or any(
        not isinstance(version, str) or not version for version in tested_harnesses.values()
    ):
        raise ValueError("manifest tested_harnesses is invalid")
    wheels = {client_wheel}
    distributions: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("manifest dependency entries must be objects")  # noqa: TRY004
        if set(dependency) != {"distribution", "version", "wheel"}:
            raise ValueError("manifest dependency keys do not match the schema")
        distribution = dependency.get("distribution")
        version = dependency.get("version")
        if not isinstance(distribution, str) or not distribution:
            raise ValueError("manifest dependency distribution must be a non-empty string")
        normalized_distribution = _normalized_distribution(distribution)
        if distribution != normalized_distribution:
            raise ValueError(f"manifest dependency distribution is not normalized: {distribution}")
        if normalized_distribution in distributions:
            raise ValueError(f"duplicate manifest dependency distribution: {distribution}")
        distributions.add(normalized_distribution)
        if not isinstance(version, str) or not version:
            raise ValueError("manifest dependency version must be a non-empty string")
        wheel = _safe_relative(dependency.get("wheel"), "manifest dependency wheel")
        if _wheel_identity(root, wheel) != (normalized_distribution, version):
            raise ValueError(f"manifest dependency does not match wheel metadata: {distribution}")
        if wheel in wheels:
            raise ValueError(f"manifest wheel path collision: {wheel}")
        wheels.add(wheel)
    if any(
        len(PurePosixPath(wheel).parts) != 2
        or PurePosixPath(wheel).parts[0] != "wheels"
        or not wheel.endswith(".whl")
        for wheel in wheels
    ):
        raise ValueError("every manifest wheel must be a .whl directly under wheels/")
    return wheels


def verify_client_kit(root: Path) -> dict[str, object]:
    """Validate manifest, every digest, and the complete regular-file set."""
    files = _regular_files(root)
    hashes = _read_hashes(root)
    expected_files = set(hashes) | {"SHA256SUMS"}
    if files != expected_files:
        missing = sorted(expected_files - files)
        extra = sorted(files - expected_files)
        raise ValueError(f"Client Kit file set mismatch: missing={missing}, extra={extra}")
    for relative, expected_digest in hashes.items():
        actual_digest = _sha256(root / relative)
        if actual_digest != expected_digest:
            raise ValueError(f"Client Kit digest mismatch: {relative}")
    manifest = _read_manifest(root)
    manifest_wheels = _manifest_wheels(root, manifest)
    actual_wheels = {
        relative
        for relative in files
        if relative.startswith("wheels/") and relative.endswith(".whl")
    }
    if actual_wheels != manifest_wheels:
        raise ValueError(
            "manifest wheel file set mismatch: "
            f"missing={sorted(manifest_wheels - actual_wheels)}, "
            f"extra={sorted(actual_wheels - manifest_wheels)}"
        )
    for required in (
        "MANIFEST.json",
        "verify_client_kit.py",
        "skills/submit-gpu-task/SKILL.md",
        "config/mcp.example.json",
        "config/codex-mcp.example.toml",
        "config/dsh-mcp.example.patch.yml",
        "docs/submitting-from-an-agent-client.md",
    ):
        if required not in files:
            raise ValueError(f"Client Kit required file is missing: {required}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1:
        raise SystemExit("usage: verify_client_kit.py [KIT_ROOT]")
    root = Path(arguments[0]) if arguments else Path.cwd()
    try:
        verify_client_kit(root)
    except ValueError as exc:
        raise SystemExit(f"Client Kit verification failed: {exc}") from exc
    print(f"Client Kit verified: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
