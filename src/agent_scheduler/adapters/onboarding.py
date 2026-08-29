"""Single source of truth for how each Agent harness reaches the Submitter MCP server."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

CANONICAL_SKILL_DIR = ".agents/skills/submit-gpu-task"

HARNESSES = ("claude", "codex", "pi", "dsh")


class OnboardingError(ValueError):
    pass


@dataclass(frozen=True)
class SkillFrontmatter:
    name: str
    description: str


def read_skill_frontmatter(path: Path) -> SkillFrontmatter:
    """Parse the leading `---` block of a SKILL.md.

    Deliberately hand-rolled: the skill contract is two flat string keys, and the
    project ships no YAML dependency.
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise OnboardingError(f"skill has no frontmatter block: {path}")
    _, _, remainder = text.partition("---\n")
    block, separator, _ = remainder.partition("\n---\n")
    if not separator:
        raise OnboardingError(f"skill frontmatter block is unterminated: {path}")
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        key, delimiter, value = line.partition(":")
        if not delimiter:
            raise OnboardingError(f"skill frontmatter line is not a key/value pair: {line!r}")
        fields[key.strip()] = value.strip()
    missing = {"name", "description"} - fields.keys()
    if missing:
        raise OnboardingError(f"skill frontmatter is missing {sorted(missing)}: {path}")
    return SkillFrontmatter(name=fields["name"], description=fields["description"])


@dataclass(frozen=True)
class OnboardingConfig:
    harness: str
    files: dict[Path, str]
    argv: tuple[str, ...]
    env: dict[str, str]


_TEMPLATE_TOKEN = re.compile(r"@@[A-Z_]+@@")


def _render_template(source: Path, values: dict[str, str]) -> str:
    try:
        mode = source.lstat().st_mode
    except OSError as exc:
        raise OnboardingError(f"configuration template is missing: {source}") from exc
    if not stat.S_ISREG(mode):
        raise OnboardingError(f"configuration template must be a regular file: {source}")
    for name, value in values.items():
        if not value or any(character in {'"', "\\"} or ord(character) < 32 for character in value):
            raise OnboardingError(
                f"{name} must not contain a quote, backslash, or control character"
            )
    text = source.read_text(encoding="utf-8")
    expected = {f"@@{name}@@" for name in values}
    found = set(_TEMPLATE_TOKEN.findall(text))
    if found != expected:
        raise OnboardingError(
            f"configuration template tokens do not match the allowlist: {sorted(found)}"
        )
    for name, value in values.items():
        text = text.replace(f"@@{name}@@", value)
    if _TEMPLATE_TOKEN.search(text):
        raise OnboardingError("rendered configuration contains an unresolved token")
    return text


def build_onboarding(
    harness: str,
    *,
    output_dir: Path,
    workspace: Path,
    base_url: str,
    username: str,
    client_entrypoint: Path,
    ca_file: Path,
    config_source: Path,
) -> OnboardingConfig:
    """Render one Kit template into an isolated per-run harness configuration."""
    if harness not in HARNESSES:
        raise OnboardingError(f"unknown harness {harness!r}; expected one of {list(HARNESSES)}")
    try:
        config_mode = config_source.lstat().st_mode
    except OSError as exc:
        raise OnboardingError(f"configuration source is missing: {config_source}") from exc
    if not stat.S_ISDIR(config_mode):
        raise OnboardingError(f"configuration source must be a regular directory: {config_source}")
    values = {
        "CLIENT_ENTRYPOINT": str(client_entrypoint),
        "MASTER_URL": base_url,
        "USERNAME": username,
        "CA_FILE": str(ca_file),
        "CLIENT_WORKSPACE": str(workspace),
    }
    if harness in {"claude", "pi"}:
        path = output_dir / "mcp.json"
        content = _render_template(config_source / "mcp.example.json", values)
        return OnboardingConfig(
            harness=harness,
            files={path: content},
            argv=("--strict-mcp-config", "--mcp-config", str(path)) if harness == "claude" else (),
            env={} if harness == "claude" else {"PI_CODING_AGENT_DIR": str(output_dir)},
        )
    if harness == "codex":
        content = _render_template(config_source / "codex-mcp.example.toml", values)
        codex_home = output_dir / "codex-home"
        config_toml = codex_home / "config.toml"
        return OnboardingConfig(
            harness=harness,
            files={config_toml: content},
            argv=(),
            env={"CODEX_HOME": str(codex_home)},
        )
    path = output_dir / "submitter-mcp.patch.yml"
    content = _render_template(config_source / "dsh-mcp.example.patch.yml", values)
    return OnboardingConfig(
        harness=harness,
        files={path: content},
        argv=("--patch", str(path)),
        env={},
    )


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _regular_skill_entries(source: Path) -> list[Path]:
    try:
        source_mode = source.lstat().st_mode
    except OSError as exc:
        raise OnboardingError(f"canonical Submitter skill source is missing: {source}") from exc
    if not stat.S_ISDIR(source_mode):
        raise OnboardingError(
            f"canonical Submitter skill source must be a regular directory: {source}"
        )
    entries = sorted(source.rglob("*"), key=lambda path: path.relative_to(source).as_posix())
    for entry in entries:
        mode = entry.lstat().st_mode
        if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise OnboardingError(
                f"canonical Submitter skill source entries must be regular files or directories: {entry}"
            )
    skill_file = source / "SKILL.md"
    if not _path_exists(skill_file) or not stat.S_ISREG(skill_file.lstat().st_mode):
        raise OnboardingError(f"canonical Submitter skill source is missing SKILL.md: {source}")
    return entries


def _create_claude_symlink(path: Path) -> None:
    path.symlink_to(Path("../../.agents/skills/submit-gpu-task"))


def prepare_client_workspace(skill_source: Path, workspace: Path) -> Path:
    """Publish a complete, source-checked client workspace with one atomic rename."""
    entries = _regular_skill_entries(skill_source)
    canonical = workspace / ".agents" / "skills" / "submit-gpu-task"
    claude_skill = workspace / ".claude" / "skills" / "submit-gpu-task"
    for destination in (workspace, canonical, claude_skill):
        if _path_exists(destination):
            raise OnboardingError(f"client workspace destination already exists: {destination}")

    parent = workspace.parent
    parent.mkdir(parents=True, exist_ok=True)
    if not stat.S_ISDIR(parent.lstat().st_mode):
        raise OnboardingError(f"client workspace parent must be a regular directory: {parent}")
    staging = Path(tempfile.mkdtemp(prefix=f".{workspace.name}.tmp-", dir=parent))
    try:
        staged_canonical = staging / ".agents" / "skills" / "submit-gpu-task"
        staged_canonical.mkdir(parents=True)
        for entry in entries:
            target = staged_canonical / entry.relative_to(skill_source)
            mode = entry.lstat().st_mode
            if stat.S_ISDIR(mode):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry, target)
        staged_claude = staging / ".claude" / "skills" / "submit-gpu-task"
        staged_claude.parent.mkdir(parents=True)
        _create_claude_symlink(staged_claude)
        if _path_exists(workspace):
            raise OnboardingError(f"client workspace destination already exists: {workspace}")
        os.replace(staging, workspace)
    except BaseException:
        if _path_exists(staging):
            shutil.rmtree(staging)
        raise
    return canonical


def write_onboarding(config: OnboardingConfig) -> None:
    for path, content in config.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
