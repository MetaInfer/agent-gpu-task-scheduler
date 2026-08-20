from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_scheduler.adapters.harness import FakeHarnessAdapter
from agent_scheduler.api.app import create_app
from agent_scheduler.config import Settings
from agent_scheduler.domain.compiler import CompilationContext, compile_task
from agent_scheduler.domain.models import Review, ReviewDecision, Revision, Task, new_id, utc_now
from agent_scheduler.runtime import RuntimeIdentity, init_runtime, load_runtime


def proposal_markdown(cards: int = 1, *, tbd: bool = False) -> str:
    value = "TBD" if tbd else "qualification smoke"
    return f"""# Proposal

## Identity
zz_chentian
## Objective
{value}
## Success Criteria
all_reduce and GEMM pass
## Workload and Code
versioned qualification script
## Container
fh-sglang-deepseek-v4-flash
## Resources
GPU: {cards}
## Commands
bounded foreground torchrun
## Inputs and Mounts
/public/share
## Environment
HIP_VISIBLE_DEVICES injected by Worker
## Networking and Privileges
approved baseline
## Timeout and Cleanup
600 seconds and stop container
## Framework Logs
required
## Business Logs and Outputs
required JSON and log
## Multi-node Coordination
single Worker
## Risks and Notes
qualification profile
"""


def signed_task(identity: RuntimeIdentity, cards: int = 1) -> Task:
    proposal_id = new_id("prop")
    revision = Revision(
        revision_id=new_id("rev"),
        proposal_id=proposal_id,
        number=1,
        markdown=proposal_markdown(cards),
        created_at=utc_now(),
    )
    facts = FakeHarnessAdapter().process(revision)
    review = Review(
        review_id=new_id("review"),
        proposal_id=proposal_id,
        revision_id=revision.revision_id,
        decision=ReviewDecision.APPROVE,
        rationale="approved",
        created_at=utc_now(),
    )
    context = CompilationContext(
        task_id=new_id("task"),
        execution_id=new_id("exec"),
        proposal_id=proposal_id,
        revision_id=revision.revision_id,
        facts_id=facts.facts_id,
        review_id=review.review_id,
        created_at=utc_now(),
        key_id=identity.key_id,
        policy_version="policy-v1",
        max_workers=1,
        allowed_worker_ids=("worker-local-01",),
        allowed_container=(
            "worker-local-01",
            "fh-sglang-deepseek-v4-flash",
            "zz_chentian",
        ),
        allowed_image_digest=facts.image_digest,
    )
    return compile_task(revision, facts, review, context, identity.signing_private_key)


@pytest.fixture
def runtime_identity(tmp_path: Path) -> tuple[Path, RuntimeIdentity]:
    root = tmp_path / "state"
    init_runtime(root)
    return root, load_runtime(root)


@pytest.fixture
def app_client(runtime_identity: tuple[Path, RuntimeIdentity]):
    root, identity = runtime_identity
    settings = Settings(
        state_root=root,
        harness_mode="fake",
        worker_mode="fake",
        qualification_profile=False,
        vram_threshold=2.0,
        allowed_users=frozenset({"zz_chentian"}),
    )
    app = create_app(settings, identity)
    with TestClient(app) as client:
        yield client, app
