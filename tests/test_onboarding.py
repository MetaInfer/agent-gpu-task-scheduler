from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_scheduler_client.tools import SUBMITTER_TOOLS

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from agent_scheduler.adapters.onboarding import (
    CANONICAL_SKILL_DIR,
    HARNESSES,
    OnboardingError,
    build_onboarding,
    read_skill_frontmatter,
    write_onboarding,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_TEMPLATE_TOKENS = {
    "@@CLIENT_ENTRYPOINT@@",
    "@@MASTER_URL@@",
    "@@USERNAME@@",
    "@@CA_FILE@@",
    "@@CLIENT_WORKSPACE@@",
}


def test_canonical_skill_has_valid_frontmatter():
    skill = PROJECT_ROOT / CANONICAL_SKILL_DIR / "SKILL.md"
    frontmatter = read_skill_frontmatter(skill)
    # The Agent Skills standard requires name to match the parent directory.
    # pi relaxes this, codex and Claude ​Code do not, so hold the strict rule.
    assert frontmatter.name == skill.parent.name
    assert frontmatter.description.strip()


def test_canonical_skill_ships_the_proposal_template():
    template = PROJECT_ROOT / CANONICAL_SKILL_DIR / "reference" / "proposal-template.md"
    assert template.is_file()


def test_claude_skill_directory_points_at_the_canonical_skill():
    claude_skill = PROJECT_ROOT / ".claude" / "skills" / "submit-gpu-task"
    canonical = PROJECT_ROOT / CANONICAL_SKILL_DIR
    assert claude_skill.is_symlink()
    assert claude_skill.resolve() == canonical.resolve()


def _build(harness: str, tmp_path: Path):
    return build_onboarding(
        harness,
        output_dir=tmp_path / "run",
        workspace=tmp_path / "workspace",
        base_url="https://master.example:8443",
        username="client_user-1",
        client_entrypoint=Path("/opt/agent-client/venv/bin/agent-scheduler-submitter"),
        ca_file=Path("/shared/state/tls/certificate.pem"),
        config_source=PROJECT_ROOT / "config" / "client",
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_every_harness_reaches_the_same_mcp_command(harness: str, tmp_path: Path):
    """No Agent gets a bespoke server; the onboarding differs, the target does not."""
    config = _build(harness, tmp_path)
    rendered = json.dumps(
        {
            "argv": list(config.argv),
            "env": config.env,
            "files": {path.name: content for path, content in config.files.items()},
        }
    )
    assert "agent-scheduler-submitter" in rendered
    assert "agent_scheduler.cli.main" not in rendered
    assert "AGENT_SCHEDULER_STATE_ROOT" not in rendered
    assert "/public/share/fh/agent-gpu-task-scheduler" not in rendered
    assert "--ca-file" in rendered


@pytest.mark.parametrize("harness", HARNESSES)
def test_generated_files_land_only_under_the_output_directory(harness: str, tmp_path: Path):
    """Per-run config must never touch a user dotfile."""
    output_dir = tmp_path / "run"
    config = _build(harness, tmp_path)
    for path in config.files:
        assert path == output_dir or output_dir in path.parents


def test_claude_config_is_a_valid_strict_mcp_config(tmp_path: Path):
    config = _build("claude", tmp_path)
    write_onboarding(config)
    assert "--strict-mcp-config" in config.argv
    index = config.argv.index("--mcp-config")
    written = Path(config.argv[index + 1])
    server = json.loads(written.read_text(encoding="utf-8"))["mcpServers"]["submitter"]
    assert server["command"] == "/opt/agent-client/venv/bin/agent-scheduler-submitter"
    assert server["args"] == [
        "--base-url",
        "https://master.example:8443",
        "--username",
        "client_user-1",
        "--ca-file",
        "/shared/state/tls/certificate.pem",
    ]
    assert server["cwd"] == str(tmp_path / "workspace")
    assert server["directTools"] == list(SUBMITTER_TOOLS)
    assert len(server["directTools"]) == len(set(server["directTools"]))
    assert "env" not in server


def test_pi_reuses_the_same_mcp_json_shape_and_promotes_direct_tools(tmp_path: Path):
    config = _build("pi", tmp_path)
    written = next(path for path in config.files if path.name == "mcp.json")
    server = json.loads(config.files[written])["mcpServers"]["submitter"]
    # Without directTools the 12 tools hide behind pi-mcp-adapter's proxy tool.
    assert server["directTools"] == list(SUBMITTER_TOOLS)
    assert len(server["directTools"]) == len(set(server["directTools"]))
    assert config.env["PI_CODING_AGENT_DIR"] == str(written.parent)


def test_codex_config_is_rendered_as_the_sole_codex_home_config(tmp_path: Path):
    """The rendered TOML must be the only thing that drives the codex launch.

    No independently-synthesized `-c mcp_servers.submitter.*` argv may exist alongside it
    -- that would be a second, divergent source of truth for the same configuration.
    """
    config = _build("codex", tmp_path)
    assert config.argv == ()
    assert not any("mcp_servers.submitter" in value for value in config.argv)
    codex_home = Path(config.env["CODEX_HOME"])
    assert codex_home == tmp_path / "run" / "codex-home"
    config_toml_path, rendered = next(
        (path, content) for path, content in config.files.items() if path.suffix == ".toml"
    )
    assert config_toml_path == codex_home / "config.toml"
    server = tomllib.loads(rendered)["mcp_servers"]["submitter"]
    assert server == {
        "command": "/opt/agent-client/venv/bin/agent-scheduler-submitter",
        "args": [
            "--base-url",
            "https://master.example:8443",
            "--username",
            "client_user-1",
            "--ca-file",
            "/shared/state/tls/certificate.pem",
        ],
        "cwd": str(tmp_path / "workspace"),
    }


@pytest.mark.parametrize("bad", ['quote"value', "back\\slash", "line\nfeed", "control\x01"])
def test_template_renderer_rejects_values_unsafe_in_json_toml_and_yaml(
    bad: str, tmp_path: Path
) -> None:
    with pytest.raises(OnboardingError, match="quote, backslash, or control"):
        build_onboarding(
            "claude",
            output_dir=tmp_path / "run",
            workspace=tmp_path / "workspace",
            base_url="https://master.example:8443",
            username=bad,
            client_entrypoint=Path("/opt/agent-client/venv/bin/agent-scheduler-submitter"),
            ca_file=Path("/shared/state/tls/certificate.pem"),
            config_source=PROJECT_ROOT / "config" / "client",
        )


def test_dsh_patch_uses_the_workspace_skill_root(tmp_path: Path):
    config = _build("dsh", tmp_path)
    patch = next(iter(config.files.values()))
    assert f'          - "{tmp_path / "workspace"}/.agents/skills"' in patch
    assert "        env:" not in patch


def test_client_config_templates_use_only_documented_tokens():
    paths = (
        PROJECT_ROOT / "config" / "client" / "mcp.example.json",
        PROJECT_ROOT / "config" / "client" / "codex-mcp.example.toml",
        PROJECT_ROOT / "config" / "client" / "dsh-mcp.example.patch.yml",
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        found = {f"@@{value}@@" for value in text.split("@@")[1::2]}
        assert found == _TEMPLATE_TOKENS
        assert "agent_scheduler.cli.main" not in text
        assert "AGENT_SCHEDULER_STATE_ROOT" not in text


def test_json_and_toml_templates_parse_before_rendering():
    json.loads(
        (PROJECT_ROOT / "config" / "client" / "mcp.example.json").read_text(encoding="utf-8")
    )
    tomllib.loads(
        (PROJECT_ROOT / "config" / "client" / "codex-mcp.example.toml").read_text(encoding="utf-8")
    )


def test_dsh_template_uses_the_exact_real_plugin_directive_shape():
    text = (PROJECT_ROOT / "config" / "client" / "dsh-mcp.example.patch.yml").read_text(
        encoding="utf-8"
    )
    assert text == (
        "- insert:\n"
        "    - id: mcp-submitter\n"
        "      name: '@deepseek-ai/dsh-mcp-client'\n"
        "      config:\n"
        "        serverName: submitter\n"
        "        transport: stdio\n"
        '        command: "@@CLIENT_ENTRYPOINT@@"\n'
        '        args: ["--base-url", "@@MASTER_URL@@", "--username", '
        '"@@USERNAME@@", "--ca-file", "@@CA_FILE@@"]\n'
        '        cwd: "@@CLIENT_WORKSPACE@@"\n'
        "    - id: skill-filesystem-submitter\n"
        "      name: '@deepseek-ai/dsh-skill-filesystem'\n"
        "      config:\n"
        "        providerName: submitter\n"
        "        customSkillDirs:\n"
        '          - "@@CLIENT_WORKSPACE@@/.agents/skills"\n'
    )


def test_unknown_harness_is_rejected(tmp_path: Path):
    with pytest.raises(OnboardingError):
        _build("gemini", tmp_path)


def test_example_mcp_config_uses_the_python_module_entrypoint():
    config = json.loads(
        (PROJECT_ROOT / "config" / "submitter-mcp.example.json").read_text(encoding="utf-8")
    )
    server = config["mcpServers"]["submitter"]

    assert server["command"] == "python3"
    assert server["args"][:3] == ["-m", "agent_scheduler.cli.main", "mcp"]


def test_onboarding_docs_cover_every_harness():
    paths = (
        PROJECT_ROOT / "docs" / "submitting-from-an-agent-client.md",
        PROJECT_ROOT / "docs" / "submitting-from-an-agent-session.md",
    )
    for path in paths:
        doc = path.read_text(encoding="utf-8")
        for harness in HARNESSES:
            assert harness in doc.lower(), f"{path.name} does not mention {harness}"

    provider_doc = paths[1].read_text(encoding="utf-8")
    # Provider-side qualification keeps its historical one-time install commands.
    assert "pi install npm:pi-mcp-adapter" in provider_doc
    assert "dsh plugin" in provider_doc and "dsh-mcp-bridge" in provider_doc


def test_no_document_still_points_at_the_old_claude_only_filename():
    # `.superpowers/` is gitignored SDD run scratch (this task's own brief/report
    # necessarily quote the old filename while describing the rename).
    # `docs/superpowers/` holds historical SDD specs and plans: point-in-time records
    # of decisions as they were authored, not living docs expected to track renames.
    stale = []
    for path in PROJECT_ROOT.glob("**/*.md"):
        relative = path.relative_to(PROJECT_ROOT)
        if relative.parts and relative.parts[0] in {".venv", ".superpowers"}:
            continue
        if relative.parts[:2] in {(".claude", "worktrees"), ("docs", "superpowers")}:
            continue
        if "submitting-from-a-claude-session" in path.read_text(encoding="utf-8"):
            stale.append(str(relative))
    assert stale == []
