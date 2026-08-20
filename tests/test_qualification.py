from pathlib import Path

from agent_scheduler.qualification import (
    QualificationItem,
    QualificationResult,
    run_submitter_agent,
    verify_qualification,
)


def test_missing_api_key_is_structured_block(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = run_submitter_agent(
        project_root=Path.cwd(),
        state_root=tmp_path / "state",
        base_url="https://127.0.0.1:8443",
        tls_certificate=tmp_path / "missing.pem",
    )
    assert result.status == "BLOCKED_QUALIFICATION"
    assert "ANTHROPIC_API_KEY" in (result.reason or "")
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
