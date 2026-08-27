"""Test fixture: launch each Agent harness as a Submitter.

This is not a production abstraction. Submitter Agents are outside our control;
this module exists only so the qualification run can prove the onboarding surface
actually works from all four harnesses.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agent_scheduler.adapters.onboarding import (
    HARNESSES,
    SUBMITTER_TOOLS,
    OnboardingError,
    build_onboarding,
    write_onboarding,
)

_EXECUTABLES = {"claude": "claude", "codex": "codex", "pi": "pi", "dsh": "dsh"}


@dataclass(frozen=True)
class SubmitterInvocation:
    argv: tuple[str, ...]
    env: dict[str, str]
    prompt: str


def submitter_executable(harness: str) -> str:
    try:
        return _EXECUTABLES[harness]
    except KeyError as exc:
        raise OnboardingError(f"unknown harness {harness!r}") from exc


def build_submitter_invocation(
    harness: str,
    *,
    prompt_kind: Literal["qualification", "connectivity"] = "qualification",
    output_dir: Path,
    project_root: Path,
    state_root: Path,
    base_url: str,
    username: str,
    python_path: Path,
    tls_certificate: Path,
    executable: str | None = None,
    run_id: str,
) -> SubmitterInvocation:
    if harness not in HARNESSES:
        raise OnboardingError(f"unknown harness {harness!r}; expected one of {list(HARNESSES)}")
    onboarding = build_onboarding(
        harness,
        output_dir=output_dir,
        project_root=project_root,
        state_root=state_root,
        base_url=base_url,
        username=username,
        python_path=python_path,
    )
    write_onboarding(onboarding)
    pi_model_arguments: tuple[str, ...] = ()
    pi_provider: str | None = None
    if harness == "pi":
        pi_model_arguments = _pi_model_arguments()
        pi_provider = pi_model_arguments[1]
        _seed_pi_agent_dir(output_dir)
    binary = executable or submitter_executable(harness)
    prompt = _prompt(project_root, run_id, prompt_kind)
    env = _base_environment(tls_certificate, harness=harness, pi_provider=pi_provider)
    env.update(onboarding.env)
    if harness == "claude":
        allowed_tools = ",".join(f"mcp__submitter__{tool}" for tool in SUBMITTER_TOOLS)
        argv = (
            binary,
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
            allowed_tools,
            *onboarding.argv,
            "--output-format",
            "stream-json",
            "--verbose",
        )
    elif harness == "codex":
        argv = (
            binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C",
            str(project_root),
            *onboarding.argv,
        )
    elif harness == "pi":
        adapter = _pi_source_agent_dir() / "npm" / "node_modules" / "pi-mcp-adapter" / "index.ts"
        argv = (
            binary,
            *pi_model_arguments,
            "--extension",
            str(adapter),
            "--print",
            "--mode",
            "json",
            *onboarding.argv,
        )
    else:
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
        argv = (binary, "--profile", "headless", *onboarding.argv, prompt)
    return SubmitterInvocation(argv=argv, env=env, prompt=prompt)


def _prompt(
    project_root: Path,
    run_id: str,
    prompt_kind: Literal["qualification", "connectivity"],
) -> str:
    """Fold the selected system prompt into the body; codex and dsh have no flag for it."""
    prompt_name = (
        "submitter.md" if prompt_kind == "qualification" else "submitter-connectivity.md"
    )
    system_prompt = (project_root / "prompts" / prompt_name).read_text(encoding="utf-8")
    if prompt_kind == "connectivity":
        task = (
            "Create exactly ONE 1-card Proposal, then stop after create_proposal returns. "
            "Do not confirm the revision, submit a reply, or poll for a Task."
        )
    else:
        task = (
            "Run the four-task qualification: one Proposal each for 1, 2, 4, and 8 cards. "
            "Drive each Task to a terminal state before reporting."
        )
    return (
        f"{system_prompt.strip()}\n\n"
        f"qualification_run_id={run_id}\n"
        f"{task}\n"
        f"Every Proposal you create MUST include the exact line `Qualification Run: {run_id}`."
    )


_PROVIDER_ENV = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    "deepseek": ("DEEPSEEK_API_KEY",),
}


def _base_environment(
    tls_certificate: Path,
    *,
    harness: str,
    pi_provider: str | None,
) -> dict[str, str]:
    env = {
        "HOME": os.environ.get("HOME", "/root"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "DISABLE_UPDATES": "1",
        "SSL_CERT_FILE": str(tls_certificate),
    }
    provider = {
        "claude": "anthropic",
        "codex": "openai",
        "pi": pi_provider,
        "dsh": "deepseek",
    }[harness]
    for name in (*_PROVIDER_ENV.get(provider or "", ()), "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _pi_model_arguments() -> tuple[str, ...]:
    provider = os.environ.get("AGENT_SCHEDULER_PI_PROVIDER")
    model = os.environ.get("AGENT_SCHEDULER_PI_MODEL")
    if not provider or not model:
        raise OnboardingError(
            "AGENT_SCHEDULER_PI_PROVIDER and AGENT_SCHEDULER_PI_MODEL must both be set"
        )
    return ("--provider", provider, "--model", model)


def _pi_source_agent_dir() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(configured) if configured else Path(os.environ.get("HOME", "/root")) / ".pi" / "agent"


def _seed_pi_agent_dir(output_dir: Path) -> None:
    """Copy credentials into the isolated agent dir.

    pi resolves auth.json from PI_CODING_AGENT_DIR, so redirecting that variable for
    per-run isolation would otherwise drop the provider credentials with it.
    """
    source = _pi_source_agent_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("auth.json", "models-store.json"):
        candidate = source / name
        if candidate.is_file():
            shutil.copy2(candidate, output_dir / name)
