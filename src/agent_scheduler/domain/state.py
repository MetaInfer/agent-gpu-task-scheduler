"""Explicit state transition rules used by Proposal and Scheduler modules."""

from __future__ import annotations

from agent_scheduler.domain.models import ProposalState, TaskState


class InvalidTransition(ValueError):
    pass


_PROPOSAL_EDGES: dict[ProposalState, frozenset[ProposalState]] = {
    ProposalState.CLARIFYING: frozenset(
        {ProposalState.AWAITING_CONFIRMATION, ProposalState.CANCELLED, ProposalState.EXPIRED}
    ),
    ProposalState.AWAITING_CONFIRMATION: frozenset(
        {
            ProposalState.IN_REVIEW,
            ProposalState.CLARIFYING,
            ProposalState.CANCELLED,
            ProposalState.EXPIRED,
        }
    ),
    ProposalState.IN_REVIEW: frozenset(
        {
            ProposalState.APPROVED,
            ProposalState.CHANGES_REQUESTED,
            ProposalState.REJECTED,
            ProposalState.PROCESSING_ERROR,
            ProposalState.CANCELLED,
            ProposalState.EXPIRED,
        }
    ),
    ProposalState.CHANGES_REQUESTED: frozenset(
        {ProposalState.CLARIFYING, ProposalState.CANCELLED, ProposalState.EXPIRED}
    ),
    ProposalState.APPROVED: frozenset({ProposalState.COMPILING, ProposalState.CANCELLED}),
    ProposalState.COMPILING: frozenset(
        {ProposalState.COMPILED, ProposalState.COMPILE_FAILED, ProposalState.CANCELLED}
    ),
    ProposalState.COMPILE_FAILED: frozenset({ProposalState.COMPILING, ProposalState.CANCELLED}),
    ProposalState.PROCESSING_ERROR: frozenset({ProposalState.CLARIFYING, ProposalState.CANCELLED}),
    ProposalState.COMPILED: frozenset(),
    ProposalState.REJECTED: frozenset(),
    ProposalState.CANCELLED: frozenset(),
    ProposalState.EXPIRED: frozenset(),
}

_TASK_EDGES: dict[TaskState, frozenset[TaskState]] = {
    TaskState.CREATED: frozenset({TaskState.QUEUED, TaskState.BLOCKED, TaskState.FAILED}),
    TaskState.QUEUED: frozenset({TaskState.PREPARING, TaskState.CANCELLED, TaskState.BLOCKED}),
    TaskState.BLOCKED: frozenset({TaskState.QUEUED, TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.PREPARING: frozenset(
        {
            TaskState.QUEUED,
            TaskState.RESERVED,
            TaskState.FINALIZING,
            TaskState.RECONCILIATION_REQUIRED,
        }
    ),
    TaskState.RESERVED: frozenset(
        {TaskState.DISPATCHED, TaskState.FINALIZING, TaskState.RECONCILIATION_REQUIRED}
    ),
    TaskState.DISPATCHED: frozenset(
        {TaskState.STARTING, TaskState.FINALIZING, TaskState.RECONCILIATION_REQUIRED}
    ),
    TaskState.STARTING: frozenset(
        {TaskState.RUNNING, TaskState.FINALIZING, TaskState.RECONCILIATION_REQUIRED}
    ),
    TaskState.RUNNING: frozenset({TaskState.FINALIZING, TaskState.RECONCILIATION_REQUIRED}),
    TaskState.FINALIZING: frozenset(
        {
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.CLEANUP_FAILED,
            TaskState.RECONCILIATION_REQUIRED,
        }
    ),
    TaskState.COMPLETED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.CLEANUP_FAILED: frozenset(
        {TaskState.RECONCILIATION_REQUIRED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RECONCILIATION_REQUIRED: frozenset({TaskState.FAILED, TaskState.CANCELLED}),
}


def transition_proposal(current: ProposalState, target: ProposalState) -> ProposalState:
    if target not in _PROPOSAL_EDGES[current]:
        raise InvalidTransition(f"proposal {current.value} -> {target.value} is not allowed")
    return target


def transition_task(current: TaskState, target: TaskState) -> TaskState:
    if target not in _TASK_EDGES[current]:
        raise InvalidTransition(f"task {current.value} -> {target.value} is not allowed")
    return target


def task_targets(current: TaskState) -> set[TaskState]:
    return set(_TASK_EDGES[current])
