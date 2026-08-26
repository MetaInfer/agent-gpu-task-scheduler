from pathlib import Path

from conftest import proposal_markdown, signed_task

from agent_scheduler.domain.models import (
    Revision,
    TaskState,
    TaskStatus,
    new_id,
    utc_now,
)
from agent_scheduler.qualification import (
    QualificationItem,
    QualificationResult,
    run_submitter_agent,
    verify_qualification,
)
from agent_scheduler.storage import EventStore


def test_missing_credentials_are_a_structured_block(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    result = run_submitter_agent(
        project_root=Path.cwd(),
        state_root=tmp_path / "state",
        base_url="https://127.0.0.1:8443",
        tls_certificate=tmp_path / "missing.pem",
    )
    assert result.status == "BLOCKED_QUALIFICATION"
    assert "ANTHROPIC_API_KEY" in (result.reason or "")
    assert "ANTHROPIC_AUTH_TOKEN" in (result.reason or "")
    assert result.run_id.startswith("qual_")


def test_unbound_historical_result_cannot_pass(runtime_identity):
    root, identity = runtime_identity
    fake = QualificationResult(
        run_id="qual_01900000000070008000000000000000",
        status="COMPLETED",
        items=(
            QualificationItem(
                card_count=1,
                proposal_id="prop_old",
                task_id="task_old",
                state="COMPLETED",
            ),
            QualificationItem(
                card_count=2,
                proposal_id="prop_old2",
                task_id="task_old2",
                state="COMPLETED",
            ),
            QualificationItem(
                card_count=4,
                proposal_id="prop_old4",
                task_id="task_old4",
                state="COMPLETED",
            ),
            QualificationItem(
                card_count=8,
                proposal_id="prop_old8",
                task_id="task_old8",
                state="COMPLETED",
            ),
        ),
    )
    verified = verify_qualification(fake, state_root=root, identity=identity)
    assert verified.status == "BLOCKED_QUALIFICATION"


def _seed_qualification_item(
    store: EventStore,
    identity,
    run_id: str,
    cards: int,
    state: str = "COMPLETED",
) -> None:
    """Write the minimum Ground Truth the reconstruction reads: a revision bound to
    the run, a COMPILED Proposal snapshot, the Task, and its latest status."""
    task = signed_task(identity, cards)
    revision = Revision(
        revision_id=task.revision_id,
        proposal_id=task.proposal_id,
        number=1,
        markdown=f"{proposal_markdown(cards)}\nQualification Run: {run_id}\n",
        created_at=utc_now(),
    )
    store.write_immutable("revisions", revision.revision_id, revision)
    store.write_snapshot(
        "proposals",
        task.proposal_id,
        {"proposal_id": task.proposal_id, "state": "COMPILED"},
    )
    store.write_immutable("tasks", task.task_id, task)
    store.write_immutable(
        "task-status-history",
        new_id("status"),
        TaskStatus(
            task_id=task.task_id,
            execution_id=task.execution_id,
            state=TaskState(state),
            updated_at=utc_now(),
        ),
    )


def test_reconstruction_finds_every_proposal_bound_to_the_run(runtime_identity):
    """The Agent is not asked for its result; the run_id binding already in the
    evidence graph is used to discover it."""
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    run_id = "qual_reconstruct"
    for cards in (1, 2, 4, 8):
        _seed_qualification_item(store, identity, run_id, cards)

    result = reconstruct_qualification_result(store, run_id)

    assert result.run_id == run_id
    assert result.status == "COMPLETED", result.reason
    assert sorted(item.card_count for item in result.items) == [1, 2, 4, 8]
    assert all(item.state == "COMPLETED" for item in result.items)


def test_reconstruction_reports_the_missing_card_counts(runtime_identity):
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    run_id = "qual_partial"
    for cards in (1, 2):
        _seed_qualification_item(store, identity, run_id, cards)

    result = reconstruct_qualification_result(store, run_id)

    assert result.status == "BLOCKED_QUALIFICATION"
    assert result.reason is not None and "4" in result.reason and "8" in result.reason
    assert sorted(item.card_count for item in result.items) == [1, 2]


def test_reconstruction_ignores_proposals_from_another_run(runtime_identity):
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    for cards in (1, 2, 4, 8):
        _seed_qualification_item(store, identity, "qual_other", cards)
    _seed_qualification_item(store, identity, "qual_target", 1)

    result = reconstruct_qualification_result(store, "qual_target")

    assert [item.card_count for item in result.items] == [1]


def test_reconstruction_keeps_a_task_that_did_not_complete(runtime_identity):
    from agent_scheduler.qualification import reconstruct_qualification_result

    root, identity = runtime_identity
    store = EventStore(root)
    run_id = "qual_failed"
    for cards in (1, 2, 4):
        _seed_qualification_item(store, identity, run_id, cards)
    _seed_qualification_item(store, identity, run_id, 8, state="FAILED")

    result = reconstruct_qualification_result(store, run_id)

    assert result.status == "BLOCKED_QUALIFICATION"
    assert {item.card_count: item.state for item in result.items}[8] == "FAILED"
