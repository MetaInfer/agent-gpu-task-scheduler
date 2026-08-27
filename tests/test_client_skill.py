from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / ".agents" / "skills" / "submit-gpu-task"


def test_client_skill_uses_semantic_tools_and_provider_fault_boundary():
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "mcp__submitter__*" not in text
    assert "agent-scheduler serve" not in text
    assert "agent-scheduler worker" not in text
    assert "contact the service provider" in text
    for tool in ("create_proposal", "reply", "confirm_revision", "wait_for_task"):
        assert tool in text


def test_proposal_template_uses_configured_identity_placeholder():
    text = (SKILL_ROOT / "reference" / "proposal-template.md").read_text(
        encoding="utf-8"
    )
    assert "Submitter username: `<submitter-username>`." in text
    assert "Submitter username: `zz_chentian`." not in text
    assert "create_proposal" in text
    assert "configured submitter username" in text
