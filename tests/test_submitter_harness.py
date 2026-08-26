from __future__ import annotations

from pathlib import Path

import pytest

from agent_scheduler.adapters.onboarding import HARNESSES
from agent_scheduler.adapters.submitter import build_submitter_invocation

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _invoke(harness: str, tmp_path: Path):
    return build_submitter_invocation(
        harness,
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=Path("/public/share/agent-scheduler-mvp"),
        base_url="https://127.0.0.1:8443",
        username="zz_chentian",
        uv_path=Path("/usr/local/bin/uv"),
        tls_certificate=Path("/public/share/agent-scheduler-mvp/tls/certificate.pem"),
        run_id="qual_abc123",
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_prompt_carries_the_system_prompt_and_the_run_binding(harness: str, tmp_path: Path):
    """codex and dsh have no system-prompt flag, so every harness folds it into the body."""
    invocation = _invoke(harness, tmp_path)
    submitter_prompt = (PROJECT_ROOT / "prompts" / "submitter.md").read_text(encoding="utf-8")
    assert submitter_prompt.strip() in invocation.prompt
    assert "Qualification Run: qual_abc123" in invocation.prompt


@pytest.mark.parametrize("harness", HARNESSES)
def test_invocation_starts_with_the_harness_executable(harness: str, tmp_path: Path):
    invocation = _invoke(harness, tmp_path)
    assert invocation.argv[0] == {"claude": "claude", "codex": "codex", "pi": "pi", "dsh": "dsh"}[
        harness
    ]


@pytest.mark.parametrize("harness", HARNESSES)
def test_tls_certificate_is_exported_so_the_adapter_can_verify_the_master(
    harness: str, tmp_path: Path
):
    invocation = _invoke(harness, tmp_path)
    assert invocation.env["SSL_CERT_FILE"].endswith("tls/certificate.pem")


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


def test_pi_runs_non_interactively(tmp_path: Path):
    invocation = _invoke("pi", tmp_path)
    assert "--print" in invocation.argv


def test_pi_invocation_carries_credentials_into_the_isolated_agent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "home" / ".pi" / "agent"
    source.mkdir(parents=True)
    (source / "auth.json").write_text('{"providers": {}}', encoding="utf-8")
    (source / "models-store.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    invocation = _invoke("pi", tmp_path)

    isolated = Path(invocation.env["PI_CODING_AGENT_DIR"])
    assert (isolated / "auth.json").is_file()
    assert (isolated / "models-store.json").is_file()
