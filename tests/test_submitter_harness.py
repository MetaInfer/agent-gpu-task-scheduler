from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_scheduler_client.tools import SUBMITTER_TOOLS

from agent_scheduler.adapters import onboarding as onboarding_module
from agent_scheduler.adapters.onboarding import (
    HARNESSES,
    OnboardingError,
    prepare_client_workspace,
)
from agent_scheduler.adapters.submitter import build_submitter_invocation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolate_submitter_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "pi-source-agent"
    source.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(source))
    monkeypatch.setenv("AGENT_SCHEDULER_PI_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_SCHEDULER_PI_MODEL", "claude-test")
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.setenv(name, f"fake-{name.lower()}")


def _invoke(harness: str, tmp_path: Path, *, prompt_kind: str = "qualification"):
    return build_submitter_invocation(
        harness,
        prompt_kind=prompt_kind,
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        skill_source=PROJECT_ROOT / ".agents" / "skills" / "submit-gpu-task",
        config_source=PROJECT_ROOT / "config" / "client",
        client_workspace=tmp_path / "client-workspace",
        base_url="https://127.0.0.1:8443",
        username="zz_chentian",
        client_entrypoint=Path("/opt/agent-client/venv/bin/agent-scheduler-submitter"),
        ca_file=Path("/shared/state/tls/certificate.pem"),
        run_id="qual_abc123",
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_prompt_carries_the_system_prompt_and_the_run_binding(harness: str, tmp_path: Path):
    """codex and dsh have no system-prompt flag, so every harness folds it into the body."""
    invocation = _invoke(harness, tmp_path)
    submitter_prompt = (PROJECT_ROOT / "prompts" / "submitter.md").read_text(encoding="utf-8")
    assert submitter_prompt.strip() in invocation.prompt
    assert "Qualification Run: qual_abc123" in invocation.prompt


def test_connectivity_prompt_requests_one_unconfirmed_proposal(tmp_path: Path):
    invocation = _invoke("claude", tmp_path, prompt_kind="connectivity")

    assert "Create exactly ONE 1-card Proposal" in invocation.prompt
    assert "Do not confirm" in invocation.prompt
    assert "four-task qualification" not in invocation.prompt
    assert "Create four independent Proposals" not in invocation.prompt


@pytest.mark.parametrize("harness", HARNESSES)
def test_invocation_contains_no_server_repository_path(harness: str, tmp_path: Path):
    invocation = _invoke(harness, tmp_path)
    rendered = "\n".join(
        [
            *invocation.argv,
            *[f"{key}={value}" for key, value in sorted(invocation.env.items())],
            invocation.prompt,
            str(invocation.cwd),
        ]
    )
    assert str(PROJECT_ROOT) not in rendered
    assert invocation.cwd == tmp_path / "client-workspace"
    assert (invocation.cwd / ".agents" / "skills" / "submit-gpu-task" / "SKILL.md").is_file()
    claude_skill = invocation.cwd / ".claude" / "skills" / "submit-gpu-task"
    assert claude_skill.is_symlink()
    assert (
        claude_skill.resolve()
        == (invocation.cwd / ".agents" / "skills" / "submit-gpu-task").resolve()
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_invocation_starts_with_the_harness_executable(harness: str, tmp_path: Path):
    invocation = _invoke(harness, tmp_path)
    assert (
        invocation.argv[0]
        == {"claude": "claude", "codex": "codex", "pi": "pi", "dsh": "dsh"}[harness]
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_tls_certificate_is_exported_so_the_adapter_can_verify_the_master(
    harness: str, tmp_path: Path
):
    invocation = _invoke(harness, tmp_path)
    assert invocation.env["SSL_CERT_FILE"].endswith("tls/certificate.pem")


def test_claude_argv_allows_only_the_canonical_submitter_tools(tmp_path: Path):
    invocation = _invoke("claude", tmp_path)
    expected_tools = ",".join(f"mcp__submitter__{tool}" for tool in SUBMITTER_TOOLS)

    assert invocation.argv == (
        "claude",
        "--print",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--permission-mode",
        "dontAsk",
        "--tools",
        "",
        "--allowedTools",
        expected_tools,
        "--strict-mcp-config",
        "--mcp-config",
        str(tmp_path / "run" / "mcp.json"),
        "--output-format",
        "stream-json",
        "--verbose",
    )


def test_codex_runs_non_interactively_without_touching_the_user_config(tmp_path: Path):
    invocation = _invoke("codex", tmp_path)
    assert invocation.argv[1] == "exec"
    assert "--json" in invocation.argv
    assert "--skip-git-repo-check" in invocation.argv


def test_dsh_disables_the_approval_prompt(tmp_path: Path):
    """headless defaults to approval: ask, which deadlocks a non-interactive run."""
    invocation = _invoke("dsh", tmp_path)
    assert invocation.env["DSH_PERMISSION_MODE"] == "danger-full-access"
    assert "--profile" in invocation.argv and "headless" in invocation.argv


def test_dsh_passes_prompt_as_the_required_headless_task_argument(tmp_path: Path):
    invocation = _invoke("dsh", tmp_path)

    assert invocation.argv[-1] == invocation.prompt


def test_pi_runs_non_interactively(tmp_path: Path):
    invocation = _invoke("pi", tmp_path)
    assert "--print" in invocation.argv


def test_pi_invocation_passes_explicit_provider_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("AGENT_SCHEDULER_PI_PROVIDER", "anthropic")
    monkeypatch.setenv("AGENT_SCHEDULER_PI_MODEL", "claude-example")

    invocation = _invoke("pi", tmp_path)

    provider_index = invocation.argv.index("--provider")
    model_index = invocation.argv.index("--model")
    assert invocation.argv[provider_index + 1] == "anthropic"
    assert invocation.argv[model_index + 1] == "claude-example"


@pytest.mark.parametrize(
    ("missing", "present", "value"),
    [
        ("AGENT_SCHEDULER_PI_MODEL", "AGENT_SCHEDULER_PI_PROVIDER", "anthropic"),
        ("AGENT_SCHEDULER_PI_PROVIDER", "AGENT_SCHEDULER_PI_MODEL", "claude-test"),
    ],
)
def test_pi_invocation_rejects_partial_provider_selection(
    missing: str,
    present: str,
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(present, value)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(OnboardingError, match="must both be set"):
        _invoke("pi", tmp_path)


def test_pi_invocation_rejects_missing_provider_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("AGENT_SCHEDULER_PI_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_SCHEDULER_PI_MODEL", raising=False)

    with pytest.raises(OnboardingError, match="must both be set"):
        _invoke("pi", tmp_path)


def test_pi_invocation_carries_credentials_into_the_isolated_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "credentials-source"
    source.mkdir()
    (source / "auth.json").write_text('{"providers": {}}', encoding="utf-8")
    (source / "models-store.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(source))

    invocation = _invoke("pi", tmp_path)

    isolated = Path(invocation.env["PI_CODING_AGENT_DIR"])
    assert (isolated / "auth.json").is_file()
    assert (isolated / "models-store.json").is_file()


def test_pi_invocation_preserves_adapter_and_credentials_before_isolating_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source-agent"
    source.mkdir(parents=True)
    (source / "auth.json").write_text('{"providers": {}}', encoding="utf-8")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(source))
    monkeypatch.setenv("HOME", str(tmp_path / "different-home"))

    invocation = _invoke("pi", tmp_path)

    extension_index = invocation.argv.index("--extension")
    assert Path(invocation.argv[extension_index + 1]) == (
        source / "npm" / "node_modules" / "pi-mcp-adapter" / "index.ts"
    )
    isolated = Path(invocation.env["PI_CODING_AGENT_DIR"])
    assert isolated != source
    assert (isolated / "auth.json").is_file()


_ANTHROPIC_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
_OPENAI_ENV = {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
_DEEPSEEK_ENV = {"DEEPSEEK_API_KEY"}
_ALL_CREDENTIAL_ENV = _ANTHROPIC_ENV | _OPENAI_ENV | _DEEPSEEK_ENV


@pytest.mark.parametrize(
    ("harness", "pi_provider", "expected_credentials"),
    [
        ("claude", None, _ANTHROPIC_ENV),
        ("codex", None, _OPENAI_ENV),
        ("dsh", None, _DEEPSEEK_ENV),
        ("pi", "anthropic", _ANTHROPIC_ENV),
        ("pi", "openai", _OPENAI_ENV),
        ("pi", "deepseek", _DEEPSEEK_ENV),
    ],
)
def test_child_environment_scopes_credentials_to_the_selected_harness(
    harness: str,
    pi_provider: str | None,
    expected_credentials: set[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    if pi_provider is not None:
        monkeypatch.setenv("AGENT_SCHEDULER_PI_PROVIDER", pi_provider)
    monkeypatch.setenv("HTTP_PROXY", "http://fake-http-proxy")
    monkeypatch.setenv("HTTPS_PROXY", "http://fake-https-proxy")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    parent_names = _ALL_CREDENTIAL_ENV | {
        "HOME",
        "PATH",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "AGENT_SCHEDULER_PI_PROVIDER",
        "AGENT_SCHEDULER_PI_MODEL",
        "PI_CODING_AGENT_DIR",
    }
    parent_before = {name: os.environ.get(name) for name in parent_names}

    invocation = _invoke(harness, tmp_path)

    forwarded = _ALL_CREDENTIAL_ENV.intersection(invocation.env)
    assert forwarded == expected_credentials
    assert invocation.env["HOME"] == os.environ["HOME"]
    assert invocation.env["PATH"] == os.environ["PATH"]
    assert invocation.env["DISABLE_UPDATES"] == "1"
    assert invocation.env["SSL_CERT_FILE"].endswith("tls/certificate.pem")
    assert invocation.env["HTTP_PROXY"] == "http://fake-http-proxy"
    assert invocation.env["HTTPS_PROXY"] == "http://fake-https-proxy"
    assert invocation.env["NO_PROXY"] == "127.0.0.1"
    assert "AGENT_SCHEDULER_PI_PROVIDER" not in invocation.env
    assert "AGENT_SCHEDULER_PI_MODEL" not in invocation.env
    assert {name: os.environ.get(name) for name in parent_names} == parent_before


def _skill_source(tmp_path: Path) -> Path:
    source = tmp_path / "source-skill"
    (source / "reference").mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: submit-gpu-task\ndescription: test\n---\n", encoding="utf-8"
    )
    (source / "reference" / "proposal-template.md").write_text("template", encoding="utf-8")
    return source


def test_prepare_client_workspace_publishes_complete_tree_atomically(tmp_path: Path) -> None:
    source = _skill_source(tmp_path)
    workspace = tmp_path / "workspace"

    canonical = prepare_client_workspace(source, workspace)

    assert canonical == workspace / ".agents" / "skills" / "submit-gpu-task"
    assert (canonical / "reference" / "proposal-template.md").read_text(
        encoding="utf-8"
    ) == "template"
    claude_skill = workspace / ".claude" / "skills" / "submit-gpu-task"
    assert claude_skill.readlink() == Path("../../.agents/skills/submit-gpu-task")
    assert not list(tmp_path.glob(".workspace.tmp-*"))


@pytest.mark.parametrize("kind", ["missing", "symlink", "fifo"])
def test_prepare_client_workspace_rejects_invalid_source_before_writes(
    kind: str, tmp_path: Path
) -> None:
    source = tmp_path / "source-skill"
    if kind != "missing":
        source = _skill_source(tmp_path)
        special = source / "special"
        if kind == "symlink":
            special.symlink_to("SKILL.md")
        else:
            os.mkfifo(special)
    workspace = tmp_path / "workspace"

    with pytest.raises(OnboardingError, match="source|regular|symlink"):
        prepare_client_workspace(source, workspace)

    assert not workspace.exists()
    assert not list(tmp_path.glob(".workspace.tmp-*"))


@pytest.mark.parametrize("existing", ["workspace", "canonical", "claude"])
def test_prepare_client_workspace_never_modifies_existing_destinations(
    existing: str, tmp_path: Path
) -> None:
    source = _skill_source(tmp_path)
    workspace = tmp_path / "workspace"
    marker = workspace / "keep.txt"
    if existing == "workspace":
        workspace.mkdir()
    elif existing == "canonical":
        (workspace / ".agents" / "skills" / "submit-gpu-task").mkdir(parents=True)
    else:
        (workspace / ".claude" / "skills" / "submit-gpu-task").mkdir(parents=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(OnboardingError, match="already exists"):
        prepare_client_workspace(source, workspace)

    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("failure", ["copy", "symlink", "rename"])
def test_prepare_client_workspace_cleans_staging_on_injected_failure(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _skill_source(tmp_path)
    workspace = tmp_path / "workspace"

    if failure == "copy":
        monkeypatch.setattr(
            onboarding_module.shutil,
            "copyfile",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("copy failed")),
        )
    elif failure == "symlink":
        monkeypatch.setattr(
            onboarding_module,
            "_create_claude_symlink",
            lambda *_args: (_ for _ in ()).throw(OSError("symlink failed")),
        )
    else:
        monkeypatch.setattr(
            onboarding_module.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("rename failed")),
        )

    with pytest.raises(OSError, match=f"{failure} failed"):
        prepare_client_workspace(source, workspace)

    assert not workspace.exists()
    assert not list(tmp_path.glob(".workspace.tmp-*"))
