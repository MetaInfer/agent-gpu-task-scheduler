"""T2: prove each Agent can actually reach the control plane through its own onboarding.

Opt-in. Requires a running Master; AGENT_SCHEDULER_HARNESS_MODE=fake is enough,
so these cost a few tokens and no GPU time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from agent_scheduler.adapters.submitter import build_submitter_invocation
from agent_scheduler.domain.models import (
    Proposal,
    ProposalFacts,
    Revision,
    new_id,
    strict_from_json_value,
)
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
    python_path = Path(sys.executable).resolve()
    invocation = build_submitter_invocation(
        harness,
        prompt_kind="connectivity",
        output_dir=tmp_path / "run",
        project_root=PROJECT_ROOT,
        state_root=state_root,
        base_url=BASE_URL,
        username="zz_chentian",
        python_path=python_path,
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
    store = EventStore(state_root)
    proposal_ids_before = {
        str(value["proposal_id"])
        for value in store.iter_snapshots("proposals")
        if isinstance(value.get("proposal_id"), str)
    }
    task_ids_before = {
        str(value["task_id"])
        for value in store.iter_immutable("tasks")
        if isinstance(value.get("task_id"), str)
    }

    completed = _run_submitter(harness, tmp_path, run_id)

    # Compare the complete Ground Truth deltas so unmarked extras cannot hide behind
    # the run marker. T2 permits exactly one Proposal and no Task of any kind.
    proposal_ids_after = {
        str(value["proposal_id"])
        for value in store.iter_snapshots("proposals")
        if isinstance(value.get("proposal_id"), str)
    }
    task_ids_after = {
        str(value["task_id"])
        for value in store.iter_immutable("tasks")
        if isinstance(value.get("task_id"), str)
    }
    proposal_delta = proposal_ids_after - proposal_ids_before
    task_delta = task_ids_after - task_ids_before
    diagnostic = (
        f"{harness} violated T2's one-Proposal/create-only contract for {run_id}: "
        f"proposal_delta={sorted(proposal_delta)}, task_delta={sorted(task_delta)}\n"
        f"exit={completed.returncode}\nstdout={completed.stdout[-4000:]}\n"
        f"stderr={completed.stderr[-4000:]}"
    )
    assert len(proposal_delta) == 1, diagnostic
    assert task_delta == set(), diagnostic

    proposal_id = next(iter(proposal_delta))
    proposal_data = store.read_snapshot("proposals", proposal_id)
    assert proposal_data is not None, diagnostic
    proposal = strict_from_json_value(Proposal, proposal_data)
    assert proposal.state.value == "AWAITING_CONFIRMATION", diagnostic
    assert proposal.current_revision_id is not None, diagnostic
    assert proposal.current_facts_id is not None, diagnostic
    revision = strict_from_json_value(
        Revision,
        store.read_immutable("revisions", proposal.current_revision_id),
    )
    facts = strict_from_json_value(
        ProposalFacts,
        store.read_immutable("facts", proposal.current_facts_id),
    )
    assert f"Qualification Run: {run_id}" in revision.markdown, diagnostic
    assert revision.proposal_id == proposal_id, diagnostic
    assert facts.revision_id == revision.revision_id, diagnostic
    assert facts.required_gpu_count == 1, diagnostic
