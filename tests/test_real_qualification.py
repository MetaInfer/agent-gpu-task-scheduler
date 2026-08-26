import os
from pathlib import Path

import pytest
from conftest import proposal_markdown

from agent_scheduler.adapters.harness import ClaudeCodeAdapter
from agent_scheduler.config import QUALIFICATION_VRAM_CEILING
from agent_scheduler.domain.models import Revision, new_id, utc_now
from agent_scheduler.qualification import run_submitter_agent, verify_qualification
from agent_scheduler.runtime import load_runtime
from agent_scheduler.storage import EventStore
from agent_scheduler.worker import DockerCLI, HySmiSampler


@pytest.mark.real_claude
def test_real_claude_processor_contract(tmp_path: Path):
    if os.environ.get("RUN_REAL_CLAUDE") != "1":
        pytest.skip("set RUN_REAL_CLAUDE=1 for a billed Claude invocation")
    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        pytest.skip("real Claude roles require ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN")
    revision = Revision(
        revision_id=new_id("rev"),
        proposal_id=new_id("prop"),
        number=1,
        markdown=proposal_markdown(1),
        created_at=utc_now(),
    )
    facts = ClaudeCodeAdapter(EventStore(tmp_path / "state")).process(revision)
    assert facts.revision_id == revision.revision_id
    assert facts.required_gpu_count == 1


@pytest.mark.real_gpu
def test_real_gpu_and_container_preconditions():
    if os.environ.get("RUN_REAL_GPU") != "1":
        pytest.skip("set RUN_REAL_GPU=1 for the authorized real environment check")
    snapshots = HySmiSampler(QUALIFICATION_VRAM_CEILING).sample()
    if any(snapshot.vram_percent >= QUALIFICATION_VRAM_CEILING for snapshot in snapshots):
        pytest.skip("BLOCKED_QUALIFICATION: one or more GPUs are at or above the VRAM ceiling")
    inspection = DockerCLI().inspect("fh-sglang-deepseek-v4-flash")
    assert inspection.exists and not inspection.running


@pytest.mark.parametrize(
    ("harness", "marker_env"),
    [
        pytest.param("claude", "RUN_REAL_CLAUDE", marks=pytest.mark.real_claude),
        pytest.param("codex", "RUN_REAL_CODEX", marks=pytest.mark.real_codex),
        pytest.param("pi", "RUN_REAL_PI", marks=pytest.mark.real_pi),
        pytest.param("dsh", "RUN_REAL_DSH", marks=pytest.mark.real_dsh),
    ],
)
@pytest.mark.real_gpu
def test_complete_real_qualification(harness: str, marker_env: str):
    if os.environ.get("RUN_FULL_QUALIFICATION") != "1":
        pytest.skip("set RUN_FULL_QUALIFICATION=1 after Master and Worker are running")
    if os.environ.get(marker_env) != "1":
        pytest.skip(f"set {marker_env}=1 to qualify the {harness} Submitter")
    root = Path(os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp"))
    identity = load_runtime(root)
    result = run_submitter_agent(
        project_root=Path(__file__).resolve().parents[1],
        state_root=root,
        base_url="https://127.0.0.1:8443",
        tls_certificate=identity.tls_certificate,
        harness=harness,
    )
    verified = verify_qualification(
        result, state_root=root, identity=identity, harness=harness
    )
    assert verified.status == "COMPLETED", verified.reason
