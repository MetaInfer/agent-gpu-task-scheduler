"""Single source of truth for how each Agent harness reaches the Submitter MCP server."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from agent_scheduler_client.tools import SUBMITTER_TOOLS

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


def build_onboarding(
    harness: str,
    *,
    output_dir: Path,
    workspace: Path,
    base_url: str,
    username: str,
    client_entrypoint: Path,
    ca_file: Path,
) -> OnboardingConfig:
    """Describe how one harness is pointed at the Submitter MCP server.

    Every harness ends up invoking the same command; only the declaration
    mechanism differs. Nothing here writes outside ``output_dir``.
    """
    if harness not in HARNESSES:
        raise OnboardingError(f"unknown harness {harness!r}; expected one of {list(HARNESSES)}")
    args = [
        "--base-url",
        base_url,
        "--username",
        username,
        "--ca-file",
        str(ca_file),
    ]
    server: dict[str, object] = {
        "command": str(client_entrypoint),
        "args": args,
        "cwd": str(workspace),
        "directTools": list(SUBMITTER_TOOLS),
    }
    if harness == "claude":
        path = output_dir / "mcp.json"
        return OnboardingConfig(
            harness=harness,
            files={path: _mcp_json(server)},
            argv=("--strict-mcp-config", "--mcp-config", str(path)),
            env={},
        )
    if harness == "pi":
        path = output_dir / "mcp.json"
        return OnboardingConfig(
            harness=harness,
            files={path: _mcp_json(server)},
            argv=(),
            env={"PI_CODING_AGENT_DIR": str(output_dir)},
        )
    if harness == "codex":
        return OnboardingConfig(
            harness=harness,
            files={},
            argv=(
                "-c",
                f"mcp_servers.submitter.command={client_entrypoint}",
                "-c",
                f"mcp_servers.submitter.args={json.dumps(args)}",
                "-c",
                f'mcp_servers.submitter.cwd="{workspace}"',
            ),
            env={},
        )
    path = output_dir / "submitter-mcp.patch.yml"
    return OnboardingConfig(
        harness=harness,
        files={path: _dsh_patch(server, workspace)},
        argv=("--patch", str(path)),
        env={},
    )


def prepare_client_workspace(project_root: Path, workspace: Path) -> Path:
    source = project_root / CANONICAL_SKILL_DIR
    if not (source / "SKILL.md").is_file():
        raise OnboardingError(f"canonical Submitter skill is missing: {source}")
    canonical = workspace / ".agents" / "skills" / "submit-gpu-task"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    if canonical.exists() or canonical.is_symlink():
        raise OnboardingError(f"client skill destination already exists: {canonical}")
    shutil.copytree(source, canonical)
    claude_root = workspace / ".claude" / "skills"
    claude_root.mkdir(parents=True, exist_ok=True)
    (claude_root / "submit-gpu-task").symlink_to(
        Path("../../.agents/skills/submit-gpu-task")
    )
    return canonical


def write_onboarding(config: OnboardingConfig) -> None:
    for path, content in config.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _mcp_json(server: dict[str, object]) -> str:
    return json.dumps({"mcpServers": {"submitter": server}}, sort_keys=True, indent=2) + "\n"


def _dsh_patch(server: dict[str, object], workspace: Path) -> str:
    """Render a cordis patch overlay declaring the submitter MCP server and skill root.

    Verified against the real ``dsh-mcp-bridge`` package (its own
    ``cordis.patch.yml``) and ``dsh --profile headless --dump-config`` output,
    both empirically inspected and round-tripped through ``dsh --patch``:

    * A patch file is a list of directives; ``insert`` appends a list of
      cordis entries shaped ``id`` / ``name`` / ``config`` — *not* a bare
      flat list of entries as originally assumed.
    * The MCP server plugin's real package name is
      ``@deepseek-ai/dsh-mcp-client`` (the bundle name ``dsh-mcp-bridge`` is
      only the npm package that ships example patch entries using it), and
      its stdio config keys are ``serverName``, ``transport``, ``command``,
      ``args``, ``cwd``, ``env`` — there is no ``servers.<name>`` nesting.
    * ``@deepseek-ai/dsh-skill-filesystem`` declares extra skill roots via
      ``customSkillDirs`` (a list of paths), not ``roots``. Its provider
      name defaults to ``local``, so a second instance needs an explicit,
      distinct ``providerName`` to avoid colliding with the harness's
      built-in provider.
    """
    args = server["args"]
    assert isinstance(args, list)
    rendered_args = ", ".join(_yaml_double_quoted(str(item)) for item in args)
    return (
        "# Generated per qualification run. Do not edit; regenerate instead.\n"
        "- insert:\n"
        "    - id: mcp-submitter\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: submitter\n"
        "        transport: stdio\n"
        f"        command: {_yaml_double_quoted(str(server['command']))}\n"
        f"        args: [{rendered_args}]\n"
        f"        cwd: {_yaml_double_quoted(str(server['cwd']))}\n"
        "    - id: skill-filesystem-submitter\n"
        "      name: '@deepseek-ai/dsh-skill-filesystem'\n"
        "      config:\n"
        "        providerName: submitter\n"
        "        customSkillDirs:\n"
        f"          - {_yaml_double_quoted(str(workspace / '.agents' / 'skills'))}\n"
    )


def _yaml_double_quoted(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
