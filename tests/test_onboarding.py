from __future__ import annotations

from pathlib import Path

from agent_scheduler.adapters.onboarding import (
    CANONICAL_SKILL_DIR,
    read_skill_frontmatter,
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
