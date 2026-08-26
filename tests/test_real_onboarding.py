"""T2: prove each Agent can actually reach the control plane through its own onboarding.

Opt-in. Requires a running Master; AGENT_SCHEDULER_HARNESS_MODE=fake is enough,
so these cost a few tokens and no GPU time.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from agent_scheduler.adapters.submitter import build_submitter_invocation
from agent_scheduler.domain.models import new_id
from agent_scheduler.runtime import load_runtime
from agent_scheduler.storage import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://127.0.0.1:8443"


def _run_submitter(harness: str, tmp_path: Path, run_id: str) -> subprocess.CompletedProcess[str]:
    state_root = Path(
        os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp")
    )
    identity = load_runtime(state_root)
    response = httpx.get(
        f"{BASE_URL}/health", verify=str(identity.tls_certificate), timeout=10
    )
    response.raise_for_status()
    uv_path = shutil.which("uv")
    assert uv_path is not None, "uv is required to launch the MCP adapter"
    invocation = build_submitter_invocation(
        harness,
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=state_root,
        base_url=BASE_URL,
        username="zz_chentian",
        uv_path=Path(uv_path).resolve(),
        tls_certificate=identity.tls_certificate,
        run_id=run_id,
    )
    prompt = (
        f"{invocation.prompt}\n\n"
        "For this connectivity check, create exactly ONE 1-card Proposal and stop. "
        "Do not confirm it, do not poll for a Task."
    )
    return subprocess.run(
        list(invocation.argv),
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=15 * 60,
        cwd=PROJECT_ROOT,
        env=invocation.env,
    )


def _created_proposal_for(state_root: Path, run_id: str) -> bool:
    marker = f"Qualification Run: {run_id}"
    return any(
        isinstance(value.get("markdown"), str) and marker in value["markdown"]
        for value in EventStore(state_root).iter_immutable("revisions")
    )


@pytest.mark.parametrize(
    ("harness", "marker_env"),
    [
        pytest.param("claude", "RUN_REAL_CLAUDE", marks=pytest.mark.real_claude),
        pytest.param("codex", "RUN_REAL_CODEX", marks=pytest.mark.real_codex),
        pytest.param("pi", "RUN_REAL_PI", marks=pytest.mark.real_pi),
        pytest.param("dsh", "RUN_REAL_DSH", marks=pytest.mark.real_dsh),
    ],
)
def test_harness_creates_a_proposal_through_its_own_onboarding(
    harness: str, marker_env: str, tmp_path: Path
):
    if os.environ.get(marker_env) != "1":
        pytest.skip(f"set {marker_env}=1 for a billed {harness} invocation")
    state_root = Path(
        os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp")
    )
    run_id = new_id("t2")

    completed = _run_submitter(harness, tmp_path, run_id)

    # A created Proposal is the only assertion that holds across all four: pi hides
    # the 12 tools behind a proxy tool unless directTools is honoured, so asserting
    # native tool names would report a false failure. Creating a Proposal proves tool
    # discovery, argument passing, and control-plane connectivity at once.
    assert _created_proposal_for(state_root, run_id), (
        f"{harness} created no Proposal bound to {run_id}\n"
        f"exit={completed.returncode}\nstdout={completed.stdout[-4000:]}\n"
        f"stderr={completed.stderr[-4000:]}"
    )
