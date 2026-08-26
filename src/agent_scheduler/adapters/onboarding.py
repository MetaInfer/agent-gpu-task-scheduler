"""Single source of truth for how each Agent harness reaches the Submitter MCP server."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

CANONICAL_SKILL_DIR = ".agents/skills/submit-gpu-task"


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
