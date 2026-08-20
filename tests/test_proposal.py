from pathlib import Path

import pytest
from conftest import proposal_markdown

from agent_scheduler.adapters.harness import FakeHarnessAdapter
from agent_scheduler.domain.models import ProposalState, ReviewDecision
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
