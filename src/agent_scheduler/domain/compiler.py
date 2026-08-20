"""Deterministic conversion from approved facts into a signed Task."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_scheduler.domain.models import (
    ID,
    ProposalFacts,
    Review,
    ReviewDecision,
    Revision,
    StrictModel,
    Task,
    TaskUnit,
)
from agent_scheduler.integrity.signing import sign_model


class CompilationContext(StrictModel):
    schema_version: Literal["v1"] = "v1"
    task_id: ID
    execution_id: ID
    proposal_id: ID
    revision_id: ID
    facts_id: ID
    review_id: ID
    created_at: datetime
    key_id: str
    policy_version: str
    max_workers: int
    allowed_worker_ids: tuple[str, ...]
    allowed_container: tuple[str, str, str]
    allowed_image_digest: str


class CompileError(ValueError):
    pass


def compile_task(
    revision: Revision,
    facts: ProposalFacts,
    review: Review,
    context: CompilationContext,
    private_key: Ed25519PrivateKey,
) -> Task:
    """Compile only frozen inputs; no clock, filesystem, or network reads occur here."""
    if review.decision is not ReviewDecision.APPROVE:
        raise CompileError("only an approved review can be compiled")
    if (
        context.proposal_id,
        context.revision_id,
        context.facts_id,
        context.review_id,
    ) != (
        revision.proposal_id,
        revision.revision_id,
        facts.facts_id,
        review.review_id,
    ):
        raise CompileError("Compilation Context is not bound to the frozen inputs")
    if review.proposal_id != revision.proposal_id:
        raise CompileError("review and revision refer to different proposals")
    if review.revision_id != revision.revision_id or facts.revision_id != revision.revision_id:
        raise CompileError("revision, facts and review must refer to the same immutable revision")
    if facts.worker_count > context.max_workers:
        raise CompileError("facts request more workers than policy allows")
    if facts.required_worker_id not in context.allowed_worker_ids:
        raise CompileError("worker is not allowlisted by the frozen policy")
    expected_container = (
        facts.required_worker_id,
        facts.container_name,
        facts.submitter_username,
    )
    if expected_container != context.allowed_container:
        raise CompileError("reuse container triple is not allowlisted")
    if facts.image_digest != context.allowed_image_digest:
        raise CompileError("image digest differs from the frozen policy")
    if facts.required_gpu_count > facts.gpu_count:
        raise CompileError("requested GPUs exceed discovered worker capacity")
    unit = TaskUnit(
        unit_id=f"unit_{context.task_id.split('_', 1)[1]}",
        required_gpu_count=facts.required_gpu_count,
        worker_id=facts.required_worker_id,
        container_name=facts.container_name,
        submitter_username=facts.submitter_username,
        container_user=facts.container_user,
        image_digest=facts.image_digest,
        setup=facts.setup,
        run=facts.run,
        teardown=facts.teardown,
        required_logs=facts.required_logs,
        required_outputs=facts.required_outputs,
        timeout_seconds=facts.timeout_seconds,
    )
    unsigned = Task(
        key_id=context.key_id,
        policy_version=context.policy_version,
        task_id=context.task_id,
        execution_id=context.execution_id,
        proposal_id=revision.proposal_id,
        revision_id=revision.revision_id,
        facts_id=facts.facts_id,
        review_id=review.review_id,
        units=(unit,),
        created_at=context.created_at,
    )
    return sign_model(unsigned, private_key)
