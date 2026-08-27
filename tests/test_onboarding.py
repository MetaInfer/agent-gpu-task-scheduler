from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_scheduler.adapters.onboarding import (
    CANONICAL_SKILL_DIR,
    HARNESSES,
    OnboardingError,
    build_onboarding,
    read_skill_frontmatter,
    write_onboarding,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
        project_root=PROJECT_ROOT,
        state_root=Path("/public/share/agent-scheduler-mvp"),
        base_url="https://127.0.0.1:8443",
        username="zz_chentian",
        python_path=Path("/usr/bin/python3"),
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
    assert "agent_scheduler.cli.main" in rendered
    assert "/usr/bin/python3" in rendered
    assert "https://127.0.0.1:8443" in rendered
    assert "zz_chentian" in rendered
    assert "/public/share/agent-scheduler-mvp" in rendered


@pytest.mark.parametrize("harness", HARNESSES)
def test_generated_files_land_only_under_the_output_directory(harness: str, tmp_path: Path):
    """Per-run config must never touch a user dotfile."""
    output_dir = tmp_path / "run"
    config = _build(harness, tmp_path)
    for path in config.files:
        assert path.parent == output_dir


def test_claude_config_is_a_valid_strict_mcp_config(tmp_path: Path):
    config = _build("claude", tmp_path)
    write_onboarding(config)
    assert "--strict-mcp-config" in config.argv
    index = config.argv.index("--mcp-config")
    written = Path(config.argv[index + 1])
    server = json.loads(written.read_text(encoding="utf-8"))["mcpServers"]["submitter"]
    assert server["command"] == "/usr/bin/python3"
    assert server["args"][:3] == ["-m", "agent_scheduler.cli.main", "mcp"]
    assert server["env"]["AGENT_SCHEDULER_STATE_ROOT"] == "/public/share/agent-scheduler-mvp"


def test_pi_reuses_the_same_mcp_json_shape_and_promotes_direct_tools(tmp_path: Path):
    config = _build("pi", tmp_path)
    written = next(path for path in config.files if path.name == "mcp.json")
    server = json.loads(config.files[written])["mcpServers"]["submitter"]
    # Without directTools the 12 tools hide behind pi-mcp-adapter's proxy tool.
    assert set(server["directTools"]) == {
        "create_proposal",
        "reply",
        "confirm_revision",
        "get_reviews",
        "resume",
        "cancel",
        "get_proposal",
        "get_task",
        "cancel_task",
        "wait_for_task",
        "wait_for_events",
        "get_logs",
    }
    assert config.env["PI_CODING_AGENT_DIR"] == str(written.parent)


def test_codex_config_is_passed_as_flags_and_writes_no_file(tmp_path: Path):
    config = _build("codex", tmp_path)
    assert config.files == {}
    assert config.argv.count("-c") == 4
    joined = " ".join(config.argv)
    assert "mcp_servers.submitter.command=/usr/bin/python3" in joined


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


def test_onboarding_doc_covers_every_harness():
    doc = (PROJECT_ROOT / "docs" / "submitting-from-an-agent-session.md").read_text(
        encoding="utf-8"
    )
    for harness in HARNESSES:
        assert harness in doc.lower(), f"onboarding doc does not mention {harness}"
    # The one-time installs are the接入方's responsibility and must be stated.
    assert "pi install npm:pi-mcp-adapter" in doc
    assert "dsh plugin" in doc and "dsh-mcp-bridge" in doc


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
