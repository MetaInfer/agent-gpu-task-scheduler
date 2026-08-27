from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLIENT_DOC = PROJECT_ROOT / "docs" / "submitting-from-an-agent-client.md"

FORBIDDEN = (
    "/public/share/fh/agent-gpu-task-scheduler",
    "python3 -m agent_scheduler.cli.main",
    "build_onboarding",
    "AGENT_SCHEDULER_HARNESS_MODE",
    "ANTHROPIC_AUTH_TOKEN",
    "reload-users",
    "init-runtime",
    "ss -lntp",
    "curl -k",
    "curl -sk",
)

REQUIRED = (
    "python3 -m venv",
    "--no-index",
    "--find-links",
    "sha256sum -c SHA256SUMS",
    "submit-gpu-task",
    "--ca-file",
    "Claude Code",
    "Codex CLI",
    "pi",
    "dsh",
    "tools/list",
    "12",
    "联系服务方",
)


def test_client_doc_is_standalone_and_contains_only_client_actions():
    text = CLIENT_DOC.read_text(encoding="utf-8")
    for value in FORBIDDEN:
        assert value not in text
    for value in REQUIRED:
        assert value in text


def test_client_doc_uses_only_the_release_version_token():
    text = CLIENT_DOC.read_text(encoding="utf-8")
    tokens = {f"@@{value}@@" for value in text.split("@@")[1::2]}
    assert tokens == {"@@KIT_VERSION@@"}


def test_provider_doc_points_client_readers_to_the_client_doc():
    text = (PROJECT_ROOT / "docs" / "submitting-from-an-agent-session.md").read_text(
        encoding="utf-8"
    )
    assert "服务提供方内部" in text
    assert "submitting-from-an-agent-client.md" in text
