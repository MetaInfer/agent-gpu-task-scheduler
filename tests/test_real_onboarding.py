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
from agent_scheduler.runtime import load_tls_certificate
from agent_scheduler.storage import EventStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://127.0.0.1:8443"


def _run_submitter(harness: str, tmp_path: Path, run_id: str) -> subprocess.CompletedProcess[str]:
    state_root = Path(
        os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp")
    )
    tls_certificate = load_tls_certificate(state_root)
    response = httpx.get(
        f"{BASE_URL}/health", verify=str(tls_certificate), timeout=10
    )
    response.raise_for_status()
    uv_path = shutil.which("uv")
    assert uv_path is not None, "uv is required to launch the MCP adapter"
    invocation = build_submitter_invocation(
        harness,
        prompt_kind="connectivity",
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=state_root,
        base_url=BASE_URL,
        username="zz_chentian",
        uv_path=Path(uv_path).resolve(),
        tls_certificate=tls_certificate,
        run_id=run_id,
    )
    return subprocess.run(
        list(invocation.argv),
        input=invocation.prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=15 * 60,
        cwd=PROJECT_ROOT,
        env=invocation.env,
    )


def _proposal_ids_for(state_root: Path, run_id: str) -> set[str]:
    marker = f"Qualification Run: {run_id}"
    return {
        str(value["proposal_id"])
        for value in EventStore(state_root).iter_immutable("revisions")
        if isinstance(value.get("proposal_id"), str)
        and isinstance(value.get("markdown"), str)
        and marker in value["markdown"]
    }


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
    run_id = new_id("t_two")

    completed = _run_submitter(harness, tmp_path, run_id)

    # Ground Truth, not CLI output, proves that connectivity mode stayed create-only.
    store = EventStore(state_root)
    proposal_ids = _proposal_ids_for(state_root, run_id)
    diagnostic = (
        f"{harness} did not create exactly one Proposal bound to {run_id}: "
        f"{sorted(proposal_ids)}\nexit={completed.returncode}\n"
        f"stdout={completed.stdout[-4000:]}\nstderr={completed.stderr[-4000:]}"
    )
    assert len(proposal_ids) == 1, diagnostic
    proposal_id = next(iter(proposal_ids))
    proposal = store.read_snapshot("proposals", proposal_id)
    assert proposal is not None and proposal.get("state") == "AWAITING_CONFIRMATION", diagnostic
    tasks = [
        value
        for value in store.iter_immutable("tasks")
        if value.get("proposal_id") == proposal_id
    ]
    assert tasks == [], diagnostic
