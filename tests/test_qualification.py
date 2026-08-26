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


def _seed_run_preconditions(
    store: EventStore, run_id: str, *, harness: str | None
) -> QualificationResult:
    """Seed just enough for verification to reach the Submitter-evidence check.

    A full passing evidence graph needs Plans, manifests, worker samples, protocol
    events and on-disk artifacts. The harness binding is decided before any of that,
    so seed up to that point and assert on which check rejects.
    """
    store.write_immutable(
        "qualification-runs",
        run_id,
        {
            "schema_version": "v1",
            "run_id": run_id,
            "started_at": utc_now().isoformat(),
            "profile": {
                "qualification": True,
                "harness_mode": "claude",
                "worker_mode": "remote",
            },
        },
    )
    store.write_immutable(
        "qualification-gates",
        new_id("gate"),
        {
            "schema_version": "v1",
            "run_id": run_id,
            "passed": True,
            "timestamp": utc_now().isoformat(),
            "results": [
                {"argv": ["uv", "run", "pytest", "-q"], "exit_code": 0},
                {"argv": ["uv", "run", "ruff", "check", "."], "exit_code": 0},
                {"argv": ["uv", "run", "mypy", "src"], "exit_code": 0},
            ],
        },
    )
    items = tuple(
        QualificationItem(
            card_count=cards,
            proposal_id=f"prop_{cards}",
            task_id=f"task_{cards}",
            state="COMPLETED",
        )
        for cards in (1, 2, 4, 8)
    )
    invocation_id = new_id("harness")
    record: dict[str, object] = {
        "schema_version": "v1",
        "invocation_id": invocation_id,
        "run_id": run_id,
        "role": "submitter",
        "exit_code": 0,
        "started_at": utc_now().isoformat(),
        "stdout": " ".join(f"{item.proposal_id} {item.task_id}" for item in items),
    }
    if harness is not None:
        record["harness"] = harness
    store.write_immutable("harness", invocation_id, record)
    return QualificationResult(run_id=run_id, status="COMPLETED", items=items)


class _StoppedDocker:
    """Stand in for DockerCLI so verification never shells out in a unit test."""

    def inspect(self, name: str):
        from agent_scheduler.worker.docker import ContainerInspection

        return ContainerInspection(exists=True, running=False)


def test_verification_rejects_submitter_evidence_from_another_harness(runtime_identity):
    from agent_scheduler.qualification import verify_qualification

    root, identity = runtime_identity
    result = _seed_run_preconditions(EventStore(root), "qual_harness", harness="codex")

    rejected = verify_qualification(
        result, state_root=root, identity=identity, harness="pi", docker=_StoppedDocker()
    )

    assert rejected.status == "BLOCKED_QUALIFICATION"
    assert rejected.reason is not None
    assert "pi Submitter evidence is incomplete" in rejected.reason


def test_verification_accepts_submitter_evidence_from_the_named_harness(runtime_identity):
    from agent_scheduler.qualification import verify_qualification

    root, identity = runtime_identity
    result = _seed_run_preconditions(EventStore(root), "qual_harness_ok", harness="codex")

    verified = verify_qualification(
        result, state_root=root, identity=identity, harness="codex", docker=_StoppedDocker()
    )

    # The graph past this point is intentionally absent, so it still blocks — but on a
    # later check, which is what proves the harness binding itself was accepted.
    assert verified.status == "BLOCKED_QUALIFICATION"
    assert verified.reason is not None
    assert "Submitter evidence is incomplete" not in verified.reason


def test_legacy_submitter_evidence_without_a_harness_field_counts_as_claude(
    runtime_identity,
):
    """Evidence recorded before the field existed was produced by Claude Code."""
    from agent_scheduler.qualification import verify_qualification

    root, identity = runtime_identity
    result = _seed_run_preconditions(EventStore(root), "qual_legacy", harness=None)

    verified = verify_qualification(
        result, state_root=root, identity=identity, harness="claude", docker=_StoppedDocker()
    )

    assert verified.reason is not None
    assert "Submitter evidence is incomplete" not in verified.reason
