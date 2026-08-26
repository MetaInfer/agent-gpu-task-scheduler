from datetime import timedelta
from pathlib import Path

import pytest
from conftest import proposal_markdown

from agent_scheduler.adapters.harness import FakeHarnessAdapter
from agent_scheduler.domain.models import (
    Command,
    CommandKind,
    Proposal,
    ProposalFacts,
    ProposalState,
    ReviewDecision,
    new_id,
    utc_now,
)
from agent_scheduler.proposal import ProposalError, ProposalService
from agent_scheduler.storage import EventStore


def service(root: Path, identity, harness: FakeHarnessAdapter) -> ProposalService:
    return ProposalService(
        EventStore(root),
        harness,
        identity.signing_private_key,
        key_id=identity.key_id,
        allowed_users={"zz_chentian"},
    )


def _minimal_proposal(proposal_id: str) -> Proposal:
    now = utc_now()
    return Proposal(
        proposal_id=proposal_id,
        username="zz_chentian",
        state=ProposalState.CLARIFYING,
        processor_rounds=1,
        review_count=0,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(days=7),
        submitter_deadline=None,
    )


def _frozen_facts(proposal_id: str, output: str, business_log: str) -> ProposalFacts:
    return ProposalFacts(
        facts_id=new_id("facts"),
        revision_id=new_id("rev"),
        worker_count=1,
        gpu_count=8,
        required_gpu_count=1,
        required_worker_id="worker-local-01",
        container_name="fh-sglang-deepseek-v4-flash",
        submitter_username="zz_chentian",
        container_user="root",
        image_digest="harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:" + "0" * 64,
        run=(
            Command(
                kind=CommandKind.CONTAINER_PATH_BASH,
                container_path="/data/fh/agent-gpu-task-scheduler/scripts/"
                "run_torch_collective_smoke.sh",
                sha256="c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c",
                argv=(output, business_log),
            ),
        ),
        required_logs=(business_log.replace("/data", "/public/share", 1),),
        required_outputs=(output.replace("/data", "/public/share", 1),),
        timeout_seconds=600,
    )


def test_reviewer_changes_requires_new_confirmation(runtime_identity):
    root, identity = runtime_identity
    harness = FakeHarnessAdapter(decisions=[ReviewDecision.REQUEST_CHANGES, ReviewDecision.APPROVE])
    proposals = service(root, identity, harness)
    created = proposals.create("zz_chentian", proposal_markdown(1), "create-1", "req-1")
    with pytest.raises(ProposalError) as error:
        proposals.confirm(
            created.proposal_id,
            "zz_chentian",
            created.current_revision_id or "",
            "confirm-1",
            "req-2",
        )
    assert error.value.code == "CHANGES_REQUESTED"
    revised = proposals.reply(
        created.proposal_id,
        "zz_chentian",
        proposal_markdown(2),
        "reply-1",
        "req-3",
    )
    task = proposals.confirm(
        created.proposal_id,
        "zz_chentian",
        revised.current_revision_id or "",
        "confirm-2",
        "req-4",
    )
    assert len(task.units) == 1
    assert task.units[0].required_gpu_count == 2
    assert proposals.get(created.proposal_id).state is ProposalState.COMPILED
    repeated = proposals.confirm(
        created.proposal_id,
        "zz_chentian",
        revised.current_revision_id or "",
        "confirm-2",
        "req-5",
    )
    assert repeated == task
    assert harness.review_calls == 2


def test_create_idempotency_does_not_repeat_harness_or_event(runtime_identity):
    root, identity = runtime_identity
    harness = FakeHarnessAdapter()
    proposals = service(root, identity, harness)
    markdown = proposal_markdown()
    first = proposals.create("zz_chentian", markdown, "same", "req-1")
    second = proposals.create("zz_chentian", markdown, "same", "req-2")
    assert first == second
    assert harness.process_calls == 1
    assert len(EventStore(root).list_events("proposals", first.proposal_id)) == 1
    with pytest.raises(ProposalError) as error:
        proposals.create("zz_chentian", proposal_markdown(2), "same", "req-3")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
    assert harness.process_calls == 1


def test_confirm_rejects_tbd(runtime_identity):
    root, identity = runtime_identity
    proposals = service(root, identity, FakeHarnessAdapter())
    created = proposals.create("zz_chentian", proposal_markdown(tbd=True), "create-tbd", "req-1")
    with pytest.raises(ProposalError, match="TBD"):
        proposals.confirm(
            created.proposal_id,
            "zz_chentian",
            created.current_revision_id or "",
            "confirm-1",
            "req-2",
        )


def test_state_rebuilds_after_snapshot_deletion(runtime_identity):
    root, identity = runtime_identity
    first = service(root, identity, FakeHarnessAdapter())
    created = first.create("zz_chentian", proposal_markdown(), "persist", "req-1")
    EventStore(root).delete_snapshot("proposals", created.proposal_id)
    rebuilt = service(root, identity, FakeHarnessAdapter())
    assert rebuilt.get(created.proposal_id) == created
    assert rebuilt.get_revision(created.proposal_id, created.current_revision_id or "")


class _FlagArgvHarness(FakeHarnessAdapter):
    """Processor that passes torchrun flags instead of the frozen two-argument form."""

    def process(self, revision):
        facts = super().process(revision)
        command = facts.run[0]
        return facts.model_copy(
            update={
                "run": (
                    command.model_copy(
                        update={
                            "argv": (
                                "--nproc-per-node",
                                "1",
                                "--output",
                                command.argv[0],
                                "--log",
                                command.argv[1],
                            )
                        }
                    ),
                )
            }
        )


def test_frozen_launcher_rejects_flag_style_argv(runtime_identity):
    root, identity = runtime_identity
    proposals = service(root, identity, _FlagArgvHarness())
    with pytest.raises(ProposalError) as error:
        proposals.create("zz_chentian", proposal_markdown(1), "create-flags", "req-flags")
    assert error.value.code == "INVALID_PROPOSAL"
    assert "frozen launcher" in str(error.value)


def test_rejected_facts_leave_no_orphan_proposal(runtime_identity):
    root, identity = runtime_identity
    proposals = service(root, identity, _FlagArgvHarness())
    with pytest.raises(ProposalError):
        proposals.create("zz_chentian", proposal_markdown(1), "create-orphan", "req-orphan")
    # The caller never received an ID, so no provisional record may survive to occupy
    # state or reappear in a rebuilt snapshot.
    assert proposals._records == {}


def test_launcher_script_and_validator_agree_on_argv_arity():
    """The validator's two-argument rule must match the launcher's own usage check."""
    script = Path("scripts/run_torch_collective_smoke.sh").read_text(encoding="utf-8")
    assert "$# -ne 2" in script
    assert "usage: %s OUTPUT BUSINESS_LOG" in script
    prompt = Path("prompts/processor.md").read_text(encoding="utf-8")
    assert "exactly two positional elements" in prompt
    assert "--nproc-per-node" in prompt


def test_frozen_launcher_rejection_states_the_expected_argv_shape(tmp_path: Path):
    """The 422 message is the only teaching channel once a Proposal is already in flight."""
    from agent_scheduler.domain.models import Command, CommandKind, ProposalFacts, new_id
    from agent_scheduler.proposal.service import ProposalError, ProposalService

    proposal_id = new_id("prop")
    facts = ProposalFacts(
        facts_id=new_id("facts"),
        revision_id=new_id("rev"),
        worker_count=1,
        gpu_count=8,
        required_gpu_count=1,
        required_worker_id="worker-local-01",
        container_name="fh-sglang-deepseek-v4-flash",
        submitter_username="zz_chentian",
        container_user="root",
        image_digest="harbor.sourcefind.cn:5443/dcu/admin/base/custom@sha256:" + "0" * 64,
        run=(
            Command(
                kind=CommandKind.CONTAINER_PATH_BASH,
                container_path="/data/fh/agent-gpu-task-scheduler/scripts/"
                "run_torch_collective_smoke.sh",
                sha256="c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c",
                argv=("--nproc-per-node", "1", "--output", "/data/out.json"),
            ),
        ),
        required_logs=("/public/share/agent-scheduler-mvp/logs/x.log",),
        required_outputs=("/public/share/agent-scheduler-mvp/outputs/x.json",),
        timeout_seconds=600,
    )
    proposal = _minimal_proposal(proposal_id)

    with pytest.raises(ProposalError) as caught:
        ProposalService._validate_facts(proposal, facts)

    message = str(caught.value)
    assert "exactly two positional arguments" in message
    assert "<container-output-path> <container-log-path>" in message
    assert "4" in message  # the count actually supplied


def test_artifact_mismatch_rejection_states_the_expected_host_paths():
    from agent_scheduler.proposal.service import ProposalError, ProposalService

    proposal_id = new_id("prop")
    output = f"/data/agent-scheduler-mvp/outputs/{proposal_id}.json"
    business_log = f"/data/agent-scheduler-mvp/logs/{proposal_id}.log"
    facts = _frozen_facts(proposal_id, output, business_log).model_copy(
        update={"required_outputs": ("/public/share/wrong.json",)}
    )

    with pytest.raises(ProposalError) as caught:
        ProposalService._validate_facts(_minimal_proposal(proposal_id), facts)

    message = str(caught.value)
    assert "/public/share/agent-scheduler-mvp/outputs/" in message
    assert "bind mount" in message
