from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PROJECT_ROOT / ".agents" / "skills" / "submit-gpu-task"


def test_client_skill_uses_semantic_tools_and_provider_fault_boundary():
    text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "mcp__submitter__" not in text
    assert "agent-scheduler serve" not in text
    assert "agent-scheduler worker" not in text
    for tool in ("create_proposal", "reply", "confirm_revision", "wait_for_task"):
        assert tool in text
    for behavior in (
        "cannot connect, stop, report the error",
        "check the issued endpoint, CA, and MCP config",
        "contact the service provider",
        "Do not start Master or Worker",
        "do not retry connectivity in a loop",
    ):
        assert behavior in normalized


def test_proposal_template_uses_configured_identity_placeholder():
    text = (SKILL_ROOT / "reference" / "proposal-template.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(text.split())
    identity_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("Submitter username:")
    ]

    assert identity_lines == ["Submitter username: `<submitter-username>`."]
    for instruction in (
        (
            "Read `<submitter-username>` (the configured submitter username) from the "
            "configured `create_proposal` tool description"
        ),
        "replace it exactly",
        "do not submit the angle-bracket token literally",
    ):
        assert instruction in normalized
