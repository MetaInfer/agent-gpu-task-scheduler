"""Proposal workflow orchestration behind a small REST/MCP-facing interface."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from threading import RLock
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_scheduler.adapters.harness import HarnessAdapter, HarnessError
from agent_scheduler.domain.compiler import CompilationContext, CompileError, compile_task
from agent_scheduler.domain.models import (
    CommandKind,
    IdempotencyRecord,
    Proposal,
    ProposalFacts,
    ProposalState,
    Review,
    ReviewDecision,
    Revision,
    Task,
    new_id,
    strict_from_json_value,
    utc_now,
)
from agent_scheduler.integrity.signing import canonical_bytes
from agent_scheduler.storage.events import EventStore

_REQUIRED_HEADINGS = (
    "# Proposal",
    "## Identity",
    "## Objective",
    "## Success Criteria",
    "## Workload and Code",
    "## Container",
    "## Resources",
    "## Commands",
    "## Inputs and Mounts",
    "## Environment",
    "## Networking and Privileges",
    "## Timeout and Cleanup",
    "## Framework Logs",
    "## Business Logs and Outputs",
    "## Multi-node Coordination",
    "## Risks and Notes",
)
_QUALIFICATION_LAUNCHER = "/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh"
_QUALIFICATION_LAUNCHER_SHA256 = "c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c"
_TERMINAL = {
    ProposalState.REJECTED,
    ProposalState.EXPIRED,
    ProposalState.CANCELLED,
    ProposalState.COMPILED,
}


class ProposalError(ValueError):
    def __init__(self, message: str, *, code: str = "INVALID_PROPOSAL") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class _Record:
    proposal: Proposal
    revisions: dict[str, Revision] = field(default_factory=dict)
    facts: dict[str, ProposalFacts] = field(default_factory=dict)
    reviews: dict[str, Review] = field(default_factory=dict)
    task: Task | None = None


class ProposalService:
    """Own workflow invariants; adapters only supply facts and decisions."""

    def __init__(
        self,
        events: EventStore,
        harness: HarnessAdapter,
        signing_key: Ed25519PrivateKey,
        *,
        key_id: str,
        allowed_users: set[str] | None = None,
        max_workers: int = 1,
        policy_version: str = "policy-v1",
    ) -> None:
        self.events = events
        self.harness = harness
        self.signing_key = signing_key
        self.key_id = key_id
        self.allowed_users = allowed_users or {"zz_chentian"}
        self.max_workers = max_workers
        self.policy_version = policy_version
        self._records: dict[str, _Record] = {}
        self._idempotency: dict[tuple[str, str, str], IdempotencyRecord] = {}
        self._lock = RLock()
        self._load()

    def create(
        self, username: str, markdown: str, idempotency_key: str, request_id: str
    ) -> Proposal:
        self._check_user(username)
        self._validate_size(markdown)
        with self._lock:
            cached = self._idempotent_result(
                username, "create", idempotency_key, {"markdown": markdown}
            )
            if cached is not None:
                return self.get(cached.object_id)
            now = utc_now()
            proposal = Proposal(
                proposal_id=new_id("prop"),
                username=username,
                state=ProposalState.CLARIFYING,
                processor_rounds=1,
                review_count=0,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(days=7),
                submitter_deadline=None,
            )
            revision = self._make_revision(proposal, markdown, 1)
            record = _Record(proposal=proposal, revisions={revision.revision_id: revision})
            self._records[proposal.proposal_id] = record
            self.events.write_immutable("revisions", revision.revision_id, revision)
            try:
                facts = self.harness.process(revision)
            except HarnessError as exc:
                record.proposal = proposal.model_copy(
                    update={"state": ProposalState.PROCESSING_ERROR, "updated_at": utc_now()}
                )
                self._persist_proposal(record.proposal)
                self.events.append(
                    "proposals",
                    proposal.proposal_id,
                    "PROCESSING_ERROR",
                    "processor",
                    request_id,
                    {"error": str(exc)},
                )
                self._save_idempotency(
                    username,
                    "create",
                    idempotency_key,
                    {"markdown": markdown},
                    proposal.proposal_id,
                    record.proposal.model_dump(mode="json"),
                )
                raise ProposalError(str(exc), code="PROCESSING_ERROR") from exc
            self._validate_facts(proposal, facts)
            self.events.write_immutable("facts", facts.facts_id, facts)
            record.facts[facts.facts_id] = facts
            deadline = utc_now() + timedelta(minutes=30)
            record.proposal = proposal.model_copy(
                update={
                    "state": ProposalState.AWAITING_CONFIRMATION,
                    "current_revision_id": revision.revision_id,
                    "current_facts_id": facts.facts_id,
                    "updated_at": utc_now(),
                    "submitter_deadline": deadline,
                }
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals",
                proposal.proposal_id,
                "CREATED",
                username,
                request_id,
                {"revision_id": revision.revision_id, "facts_id": facts.facts_id},
            )
            self._save_idempotency(
                username,
                "create",
                idempotency_key,
                {"markdown": markdown},
                proposal.proposal_id,
                record.proposal.model_dump(mode="json"),
            )
            return record.proposal

    def reply(
        self,
        proposal_id: str,
        username: str,
        markdown: str,
        idempotency_key: str,
        request_id: str,
    ) -> Proposal:
        self._check_user(username)
        self._validate_size(markdown)
        with self._lock:
            record = self._get(proposal_id)
            self._check_owner(record.proposal, username)
            self._check_lifetime(record, request_id)
            cached = self._idempotent_result(
                username,
                f"reply:{proposal_id}",
                idempotency_key,
                {"markdown": markdown},
            )
            if cached is not None:
                return record.proposal
            if record.proposal.state in _TERMINAL:
                raise ProposalError("proposal is terminal", code="TERMINAL_STATE")
            next_round = record.proposal.processor_rounds + 1
            if next_round > 8:
                raise ProposalError("processor round limit reached", code="ROUND_LIMIT")
            revision = self._make_revision(record.proposal, markdown, next_round)
            try:
                facts = self.harness.process(revision)
            except HarnessError as exc:
                record.proposal = record.proposal.model_copy(
                    update={"state": ProposalState.PROCESSING_ERROR, "updated_at": utc_now()}
                )
                self._persist_proposal(record.proposal)
                self.events.append(
                    "proposals",
                    proposal_id,
                    "PROCESSING_ERROR",
                    "processor",
                    request_id,
                    {"error": str(exc)},
                )
                raise ProposalError(str(exc), code="PROCESSING_ERROR") from exc
            self._validate_facts(record.proposal, facts)
            self.events.write_immutable("revisions", revision.revision_id, revision)
            self.events.write_immutable("facts", facts.facts_id, facts)
            record.revisions[revision.revision_id] = revision
            record.facts[facts.facts_id] = facts
            record.proposal = record.proposal.model_copy(
                update={
                    "state": ProposalState.AWAITING_CONFIRMATION,
                    "current_revision_id": revision.revision_id,
                    "current_facts_id": facts.facts_id,
                    "processor_rounds": next_round,
                    "updated_at": utc_now(),
                    "submitter_deadline": utc_now() + timedelta(minutes=30),
                }
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals",
                proposal_id,
                "REVISION_CREATED",
                username,
                request_id,
                {"revision_id": revision.revision_id, "facts_id": facts.facts_id},
            )
            self._save_idempotency(
                username,
                f"reply:{proposal_id}",
                idempotency_key,
                {"markdown": markdown},
                proposal_id,
                record.proposal.model_dump(mode="json"),
            )
            return record.proposal

    def confirm(
        self,
        proposal_id: str,
        username: str,
        revision_id: str,
        idempotency_key: str,
        request_id: str,
    ) -> Task:
        self._check_user(username)
        with self._lock:
            record = self._get(proposal_id)
            self._check_owner(record.proposal, username)
            self._check_lifetime(record, request_id)
            operation = f"confirm:{proposal_id}"
            request = {"revision_id": revision_id}
            cached = self._idempotent_result(username, operation, idempotency_key, request)
            if cached is not None:
                outcome = cached.response.get("outcome")
                if outcome == "COMPILED" and record.task is not None:
                    return record.task
                if isinstance(outcome, str):
                    raise ProposalError(f"cached confirmation outcome: {outcome}", code=outcome)
                raise ProposalError("cached confirmation has no outcome")
            if record.proposal.state is not ProposalState.AWAITING_CONFIRMATION:
                raise ProposalError("proposal is not awaiting confirmation", code="INVALID_STATE")
            if record.proposal.current_revision_id != revision_id:
                raise ProposalError("only the current revision can be confirmed")
            revision = record.revisions[revision_id]
            self._validate_review_ready(revision.markdown)
            facts = record.facts[cast(str, record.proposal.current_facts_id)]
            review_count = record.proposal.review_count + 1
            if review_count > 4:
                raise ProposalError("review limit reached", code="REVIEW_LIMIT")
            record.proposal = record.proposal.model_copy(
                update={
                    "state": ProposalState.IN_REVIEW,
                    "review_count": review_count,
                    "updated_at": utc_now(),
                    "submitter_deadline": None,
                }
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals",
                proposal_id,
                "REVISION_CONFIRMED",
                username,
                request_id,
                {"revision_id": revision_id},
            )
            try:
                review = self.harness.review(revision, facts)
            except HarnessError as exc:
                record.proposal = record.proposal.model_copy(
                    update={"state": ProposalState.PROCESSING_ERROR, "updated_at": utc_now()}
                )
                self._persist_proposal(record.proposal)
                self.events.append(
                    "proposals",
                    proposal_id,
                    "PROCESSING_ERROR",
                    "reviewer",
                    request_id,
                    {"error": str(exc)},
                )
                self._save_idempotency(
                    username,
                    operation,
                    idempotency_key,
                    request,
                    proposal_id,
                    {"outcome": "PROCESSING_ERROR"},
                )
                raise ProposalError(str(exc), code="PROCESSING_ERROR") from exc
            self.events.write_immutable("reviews", review.review_id, review)
            record.reviews[review.review_id] = review
            if review.decision is ReviewDecision.REQUEST_CHANGES:
                record.proposal = record.proposal.model_copy(
                    update={
                        "state": ProposalState.CHANGES_REQUESTED,
                        "updated_at": utc_now(),
                        "submitter_deadline": utc_now() + timedelta(minutes=30),
                    }
                )
                self._persist_proposal(record.proposal)
                self.events.append(
                    "proposals",
                    proposal_id,
                    "CHANGES_REQUESTED",
                    "reviewer",
                    request_id,
                    {"review_id": review.review_id},
                )
                self._save_idempotency(
                    username,
                    operation,
                    idempotency_key,
                    request,
                    proposal_id,
                    {"outcome": "CHANGES_REQUESTED", "review_id": review.review_id},
                )
                raise ProposalError("review requested changes", code="CHANGES_REQUESTED")
            if review.decision is ReviewDecision.REJECT:
                record.proposal = record.proposal.model_copy(
                    update={"state": ProposalState.REJECTED, "updated_at": utc_now()}
                )
                self._persist_proposal(record.proposal)
                self.events.append(
                    "proposals",
                    proposal_id,
                    "REJECTED",
                    "reviewer",
                    request_id,
                    {"review_id": review.review_id},
                )
                self._save_idempotency(
                    username,
                    operation,
                    idempotency_key,
                    request,
                    proposal_id,
                    {"outcome": "REJECTED", "review_id": review.review_id},
                )
                raise ProposalError("review rejected proposal", code="REJECTED")
            record.proposal = record.proposal.model_copy(
                update={"state": ProposalState.COMPILING, "updated_at": utc_now()}
            )
            self._persist_proposal(record.proposal)
            context = CompilationContext(
                task_id=new_id("task"),
                execution_id=new_id("exec"),
                proposal_id=proposal_id,
                revision_id=revision.revision_id,
                facts_id=facts.facts_id,
                review_id=review.review_id,
                created_at=utc_now(),
                key_id=self.key_id,
                policy_version=self.policy_version,
                max_workers=self.max_workers,
                allowed_worker_ids=("worker-local-01",),
                allowed_container=(
                    "worker-local-01",
                    "fh-sglang-deepseek-v4-flash",
                    "zz_chentian",
                ),
                allowed_image_digest=facts.image_digest,
            )
            self.events.write_immutable("compilation-contexts", context.task_id, context)
            try:
                task = compile_task(revision, facts, review, context, self.signing_key)
            except CompileError as exc:
                record.proposal = record.proposal.model_copy(
                    update={"state": ProposalState.COMPILE_FAILED, "updated_at": utc_now()}
                )
                self._persist_proposal(record.proposal)
                self.events.append(
                    "proposals",
                    proposal_id,
                    "COMPILE_FAILED",
                    "compiler",
                    request_id,
                    {"error": str(exc)},
                )
                self._save_idempotency(
                    username,
                    operation,
                    idempotency_key,
                    request,
                    proposal_id,
                    {"outcome": "COMPILE_FAILED"},
                )
                raise ProposalError(str(exc), code="COMPILE_FAILED") from exc
            self.events.write_immutable("tasks", task.task_id, task)
            self.events.write_immutable_bytes(
                "tasks", f"{task.task_id}.canonical.json", canonical_bytes(task)
            )
            record.task = task
            record.proposal = record.proposal.model_copy(
                update={"state": ProposalState.COMPILED, "updated_at": utc_now()}
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals",
                proposal_id,
                "COMPILED",
                "compiler",
                request_id,
                {"task_id": task.task_id},
            )
            self.events.append(
                "tasks",
                task.task_id,
                "CREATED",
                "compiler",
                request_id,
                {"execution_id": task.execution_id},
            )
            self._save_idempotency(
                username,
                operation,
                idempotency_key,
                request,
                proposal_id,
                {"outcome": "COMPILED", "task_id": task.task_id},
            )
            return task

    def resume(
        self, proposal_id: str, username: str, idempotency_key: str, request_id: str
    ) -> Proposal:
        self._check_user(username)
        with self._lock:
            record = self._get(proposal_id)
            self._check_owner(record.proposal, username)
            cached = self._idempotent_result(username, f"resume:{proposal_id}", idempotency_key, {})
            if cached is not None:
                return record.proposal
            if record.proposal.state is not ProposalState.PROCESSING_ERROR:
                raise ProposalError("proposal is not resumable", code="INVALID_STATE")
            target = ProposalState.CLARIFYING
            record.proposal = record.proposal.model_copy(
                update={"state": target, "updated_at": utc_now()}
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals", proposal_id, "RESUMED", username, request_id, {"state": target.value}
            )
            self._save_idempotency(
                username,
                f"resume:{proposal_id}",
                idempotency_key,
                {},
                proposal_id,
                record.proposal.model_dump(mode="json"),
            )
            return record.proposal

    def retry_compilation(self, proposal_id: str, actor: str, request_id: str) -> Task:
        """Retry only the latest frozen Compilation Context."""
        with self._lock:
            record = self._get(proposal_id)
            if record.proposal.state is not ProposalState.COMPILE_FAILED:
                raise ProposalError("proposal is not compile-failed", code="INVALID_STATE")
            contexts = [
                strict_from_json_value(CompilationContext, data)
                for data in self.events.iter_immutable("compilation-contexts")
                if data.get("proposal_id") == proposal_id
            ]
            if not contexts:
                raise ProposalError("frozen Compilation Context not found", code="NOT_FOUND")
            context = max(contexts, key=lambda item: item.created_at)
            revision = record.revisions[context.revision_id]
            facts = record.facts[context.facts_id]
            review = record.reviews[context.review_id]
            task = compile_task(revision, facts, review, context, self.signing_key)
            if self.events.immutable_exists("tasks", task.task_id):
                existing = strict_from_json_value(
                    Task, self.events.read_immutable("tasks", task.task_id)
                )
                if existing != task:
                    raise ProposalError("retry changed deterministic Task bytes")
            else:
                self.events.write_immutable("tasks", task.task_id, task)
                self.events.write_immutable_bytes(
                    "tasks", f"{task.task_id}.canonical.json", canonical_bytes(task)
                )
            record.task = task
            record.proposal = record.proposal.model_copy(
                update={"state": ProposalState.COMPILED, "updated_at": utc_now()}
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals",
                proposal_id,
                "COMPILATION_RETRIED",
                actor,
                request_id,
                {"task_id": task.task_id},
            )
            return task

    def cancel(self, proposal_id: str, username: str, request_id: str) -> Proposal:
        self._check_user(username)
        with self._lock:
            record = self._get(proposal_id)
            self._check_owner(record.proposal, username)
            if record.proposal.state in _TERMINAL:
                raise ProposalError("proposal is terminal", code="TERMINAL_STATE")
            record.proposal = record.proposal.model_copy(
                update={"state": ProposalState.CANCELLED, "updated_at": utc_now()}
            )
            self._persist_proposal(record.proposal)
            self.events.append("proposals", proposal_id, "CANCELLED", username, request_id, {})
            return record.proposal

    def get(self, proposal_id: str) -> Proposal:
        return self._get(proposal_id).proposal

    def get_revision(self, proposal_id: str, revision_id: str) -> Revision:
        return self._get(proposal_id).revisions[revision_id]

    def get_task(self, proposal_id: str) -> Task | None:
        return self._get(proposal_id).task

    def get_reviews(self, proposal_id: str) -> list[Review]:
        return sorted(self._get(proposal_id).reviews.values(), key=lambda review: review.created_at)

    def get_current_facts(self, proposal_id: str) -> ProposalFacts | None:
        record = self._get(proposal_id)
        facts_id = record.proposal.current_facts_id
        return record.facts.get(facts_id) if facts_id else None

    def list_proposals(self) -> list[Proposal]:
        return sorted(
            (record.proposal for record in self._records.values()),
            key=lambda proposal: (proposal.created_at, proposal.proposal_id),
        )

    def list_reviews(self) -> list[Review]:
        return sorted(
            (review for record in self._records.values() for review in record.reviews.values()),
            key=lambda review: (review.created_at, review.review_id),
        )

    def list_tasks(self) -> list[Task]:
        return sorted(
            (record.task for record in self._records.values() if record.task is not None),
            key=lambda task: task.task_id,
        )

    def _make_revision(self, proposal: Proposal, markdown: str, number: int) -> Revision:
        return Revision(
            revision_id=new_id("rev"),
            proposal_id=proposal.proposal_id,
            number=number,
            markdown=markdown,
            created_at=utc_now(),
        )

    def _load(self) -> None:
        self.events.validate_all_events()
        latest: dict[str, Proposal] = {}
        for data in self.events.iter_snapshots("proposals"):
            proposal = strict_from_json_value(Proposal, data)
            latest[proposal.proposal_id] = proposal
        for data in self.events.iter_immutable("proposal-states"):
            proposal = strict_from_json_value(Proposal, data)
            prior = latest.get(proposal.proposal_id)
            if prior is None or proposal.updated_at >= prior.updated_at:
                latest[proposal.proposal_id] = proposal
        self._records = {
            proposal_id: _Record(proposal=proposal) for proposal_id, proposal in latest.items()
        }
        for data in self.events.iter_immutable("revisions"):
            revision = strict_from_json_value(Revision, data)
            if revision.proposal_id in self._records:
                self._records[revision.proposal_id].revisions[revision.revision_id] = revision
        facts_by_revision: dict[str, ProposalFacts] = {}
        for data in self.events.iter_immutable("facts"):
            facts = strict_from_json_value(ProposalFacts, data)
            facts_by_revision[facts.revision_id] = facts
        for record in self._records.values():
            for revision_id in record.revisions:
                loaded_facts = facts_by_revision.get(revision_id)
                if loaded_facts:
                    record.facts[loaded_facts.facts_id] = loaded_facts
        for data in self.events.iter_immutable("reviews"):
            review = strict_from_json_value(Review, data)
            if review.proposal_id in self._records:
                self._records[review.proposal_id].reviews[review.review_id] = review
        for data in self.events.iter_immutable("tasks"):
            task = strict_from_json_value(Task, data)
            if task.proposal_id in self._records:
                self._records[task.proposal_id].task = task
        for data in self.events.iter_immutable("idempotency"):
            item = strict_from_json_value(IdempotencyRecord, data)
            self._idempotency[(item.username, item.operation, item.key)] = item

    def _persist_proposal(self, proposal: Proposal) -> None:
        self.events.write_immutable("proposal-states", new_id("prop_state"), proposal)
        self.events.write_snapshot("proposals", proposal.proposal_id, proposal)

    def _idempotent_result(
        self, username: str, operation: str, key: str, request: dict[str, Any]
    ) -> IdempotencyRecord | None:
        if not key:
            raise ProposalError("idempotency key is required", code="IDEMPOTENCY_REQUIRED")
        item = self._idempotency.get((username, operation, key))
        if item is None:
            return None
        if item.request_hash != self._request_hash(request):
            raise ProposalError(
                "idempotency key conflicts with a different request",
                code="IDEMPOTENCY_CONFLICT",
            )
        return item

    def _save_idempotency(
        self,
        username: str,
        operation: str,
        key: str,
        request: dict[str, Any],
        object_id: str,
        response: dict[str, Any],
    ) -> None:
        item = IdempotencyRecord(
            username=username,
            operation=operation,
            key=key,
            request_hash=self._request_hash(request),
            object_id=object_id,
            response=response,
        )
        snapshot_id = hashlib.sha256(f"{username}\0{operation}\0{key}".encode()).hexdigest()
        self._idempotency[(username, operation, key)] = item
        if not self.events.immutable_exists("idempotency", snapshot_id):
            self.events.write_immutable("idempotency", snapshot_id, item)

    @staticmethod
    def _request_hash(request: dict[str, Any]) -> str:
        encoded = json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _get(self, proposal_id: str) -> _Record:
        try:
            return self._records[proposal_id]
        except KeyError as exc:
            raise ProposalError("proposal not found", code="NOT_FOUND") from exc

    def _check_user(self, username: str) -> None:
        if username not in self.allowed_users:
            raise ProposalError("username is not allowlisted", code="USERNAME_NOT_ALLOWED")

    @staticmethod
    def _check_owner(proposal: Proposal, username: str) -> None:
        if proposal.username != username:
            raise ProposalError("username does not match proposal declaration")

    def _check_lifetime(self, record: _Record, request_id: str) -> None:
        now = utc_now()
        if now >= record.proposal.expires_at or (
            record.proposal.submitter_deadline is not None
            and now >= record.proposal.submitter_deadline
        ):
            record.proposal = record.proposal.model_copy(
                update={"state": ProposalState.EXPIRED, "updated_at": now}
            )
            self._persist_proposal(record.proposal)
            self.events.append(
                "proposals",
                record.proposal.proposal_id,
                "EXPIRED",
                "master",
                request_id,
                {},
            )
            raise ProposalError("proposal expired", code="EXPIRED")

    @staticmethod
    def _validate_size(markdown: str) -> None:
        if not markdown or len(markdown.encode("utf-8")) > 256 * 1024:
            raise ProposalError("proposal markdown must be 1..256 KiB")

    @staticmethod
    def _validate_review_ready(markdown: str) -> None:
        missing = [heading for heading in _REQUIRED_HEADINGS if heading not in markdown]
        if missing:
            raise ProposalError(f"proposal is missing headings: {', '.join(missing)}")
        if re.search(r"\bTBD\b", markdown, flags=re.IGNORECASE):
            raise ProposalError("confirmed revision must not contain TBD")

    @staticmethod
    def _validate_facts(proposal: Proposal, facts: ProposalFacts) -> None:
        if facts.submitter_username != proposal.username:
            raise ProposalError("Facts username differs from Proposal username")
        if facts.required_gpu_count not in {1, 2, 4, 8}:
            raise ProposalError("qualification GPU count must be 1, 2, 4, or 8")
        if len(facts.run) != 1:
            raise ProposalError("qualification requires exactly one run command")
        command = facts.run[0]
        if (
            command.kind is not CommandKind.CONTAINER_PATH_BASH
            or command.container_path != _QUALIFICATION_LAUNCHER
            or command.sha256 != _QUALIFICATION_LAUNCHER_SHA256
            or len(command.argv) != 2
        ):
            raise ProposalError("qualification run command does not match frozen launcher")
        output, business_log = command.argv
        expected_prefix = "/data/agent-scheduler-mvp/"
        if not output.startswith(f"{expected_prefix}outputs/{proposal.proposal_id}"):
            raise ProposalError("qualification output path is not Proposal-unique")
        if not business_log.startswith(f"{expected_prefix}logs/{proposal.proposal_id}"):
            raise ProposalError("qualification business log path is not Proposal-unique")
        expected_host_output = output.replace("/data", "/public/share", 1)
        expected_host_log = business_log.replace("/data", "/public/share", 1)
        if facts.required_outputs != (expected_host_output,) or facts.required_logs != (
            expected_host_log,
        ):
            raise ProposalError("required artifacts do not match launcher argv")
        if any(Path(path).exists() for path in facts.required_outputs + facts.required_logs):
            raise ProposalError("qualification artifact paths must not exist before execution")
