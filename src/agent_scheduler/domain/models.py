"""Strict versioned domain objects shared by every adapter."""

from __future__ import annotations

import json
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*_[0-9a-f]{32}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
AbsolutePath = Annotated[str, Field(pattern=r"^/(?:[A-Za-z0-9_.-]+/?)+$")]
ImageDigest = Annotated[str, Field(pattern=r"^[A-Za-z0-9./:_-]+@sha256:[0-9a-f]{64}$")]


def new_id(prefix: str) -> str:
    """Return a type-prefixed RFC 9562 UUIDv7 identifier."""
    if not prefix or any(char not in "abcdefghijklmnopqrstuvwxyz_" for char in prefix):
        raise ValueError("ID prefix must contain lowercase ASCII letters or underscores")
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp_ms << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return f"{prefix}_{uuid.UUID(int=value).hex}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=False,
        use_enum_values=False,
    )


StrictModelT = TypeVar("StrictModelT", bound=StrictModel)


def strict_from_json_value(model: type[StrictModelT], value: Any) -> StrictModelT:
    """Validate an already-decoded JSON value with strict JSON semantics."""
    return model.model_validate_json(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )


class ProposalState(str, Enum):
    CLARIFYING = "CLARIFYING"
    AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"
    IN_REVIEW = "IN_REVIEW"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    COMPILING = "COMPILING"
    COMPILED = "COMPILED"
    COMPILE_FAILED = "COMPILE_FAILED"
    PROCESSING_ERROR = "PROCESSING_ERROR"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class TaskState(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    BLOCKED = "BLOCKED"
    PREPARING = "PREPARING"
    RESERVED = "RESERVED"
    DISPATCHED = "DISPATCHED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class ReviewDecision(str, Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"


class CommandKind(str, Enum):
    INLINE_BASH = "inline_bash"
    CONTAINER_PATH_BASH = "container_path_bash"


class GpuState(str, Enum):
    AVAILABLE = "AVAILABLE"
    LEASED = "LEASED"
    DRIFTED = "DRIFTED"
    UNKNOWN = "UNKNOWN"


class Command(StrictModel):
    kind: CommandKind
    script: str | None = Field(default=None, max_length=256 * 1024)
    container_path: AbsolutePath | None = None
    sha256: Sha256 | None = None
    argv: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_variant(self) -> Command:
        if self.kind is CommandKind.INLINE_BASH:
            if self.script is None or self.container_path is not None or self.sha256 is not None:
                raise ValueError("inline_bash requires script and forbids container_path/sha256")
        elif self.kind is CommandKind.CONTAINER_PATH_BASH and (
            self.container_path is None or self.sha256 is None or self.script is not None
        ):
            raise ValueError("container_path_bash requires path and sha256")
        text = self.script or ""
        if re.search(r"(?<!&)&(?!&)|\b(?:nohup|disown|daemonize)\b", text):
            raise ValueError("background or daemonized commands are not allowed")
        if any("\x00" in arg for arg in self.argv):
            raise ValueError("command argv must not contain NUL")
        return self


class Revision(StrictModel):
    schema_version: Literal["v1"] = "v1"
    revision_id: ID
    proposal_id: ID
    number: int = Field(ge=1, le=8)
    markdown: str = Field(min_length=1, max_length=256 * 1024)
    created_at: datetime


class ProposalFacts(StrictModel):
    schema_version: Literal["v1"] = "v1"
    facts_id: ID
    revision_id: ID
    worker_count: int = Field(ge=1, le=64)
    gpu_count: int = Field(ge=1, le=8)
    gpu_type: Literal["K100_AI"] = "K100_AI"
    required_gpu_count: int = Field(ge=1, le=8)
    required_worker_id: str
    container_name: str = Field(min_length=1, max_length=128)
    submitter_username: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    container_user: str = Field(min_length=1, max_length=128)
    image_digest: ImageDigest
    setup: tuple[Command, ...] = ()
    run: tuple[Command, ...] = Field(min_length=1)
    teardown: tuple[Command, ...] = ()
    required_logs: tuple[AbsolutePath, ...] = ()
    required_outputs: tuple[AbsolutePath, ...] = ()
    timeout_seconds: int = Field(ge=1, le=7 * 24 * 3600)


class Review(StrictModel):
    schema_version: Literal["v1"] = "v1"
    review_id: ID
    proposal_id: ID
    revision_id: ID
    decision: ReviewDecision
    rationale: str = Field(min_length=1, max_length=256 * 1024)
    created_at: datetime


class TaskUnit(StrictModel):
    schema_version: Literal["v1"] = "v1"
    unit_id: ID
    required_gpu_count: int = Field(ge=1, le=8)
    worker_id: str
    container_name: str
    submitter_username: str
    container_user: str
    image_digest: ImageDigest
    setup: tuple[Command, ...] = ()
    run: tuple[Command, ...] = Field(min_length=1)
    teardown: tuple[Command, ...] = ()
    required_logs: tuple[AbsolutePath, ...] = ()
    required_outputs: tuple[AbsolutePath, ...] = ()
    timeout_seconds: int = Field(ge=1, le=7 * 24 * 3600)


class Task(StrictModel):
    """Immutable executable payload; lifecycle state is held by TaskStatus."""

    schema_version: Literal["v1"] = "v1"
    key_id: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)
    task_id: ID
    execution_id: ID
    proposal_id: ID
    revision_id: ID
    facts_id: ID
    review_id: ID
    units: tuple[TaskUnit, ...] = Field(min_length=1, max_length=64)
    created_at: datetime
    content_hash: Sha256 | None = None
    signature: str | None = None

    @model_validator(mode="after")
    def validate_task(self) -> Task:
        worker_ids = [unit.worker_id for unit in self.units]
        if len(set(worker_ids)) != len(worker_ids):
            raise ValueError("each task unit must bind a distinct worker")
        if (self.content_hash is None) != (self.signature is None):
            raise ValueError("content_hash and signature must both be present or absent")
        return self


class TaskStatus(StrictModel):
    schema_version: Literal["v1"] = "v1"
    task_id: ID
    execution_id: ID
    state: TaskState
    updated_at: datetime
    underlying_outcome: TaskState | None = None
    failure_reason: str | None = None


class GpuSnapshot(StrictModel):
    gpu_id: int = Field(ge=0, le=7)
    gpu_type: Literal["K100_AI"] = "K100_AI"
    vram_percent: float = Field(ge=0, le=100)
    sampled_at: datetime
    state: GpuState
    raw_line: str = ""


class WorkerSnapshot(StrictModel):
    worker_id: str
    online: bool
    last_heartbeat_at: datetime
    gpus: tuple[GpuSnapshot, ...] = ()


class ResourceLease(StrictModel):
    lease_id: ID
    execution_id: ID
    worker_id: str
    gpu_ids: tuple[int, ...] = Field(min_length=1, max_length=8)
    container_name: str
    lease_epoch: int = Field(ge=1)
    committed: bool = False


class PrepareManifest(StrictModel):
    schema_version: Literal["v1"] = "v1"
    key_id: str
    manifest_id: ID
    task_id: ID
    execution_id: ID
    assignment_id: ID
    dispatch_generation: int = Field(ge=1)
    worker_id: str
    gpu_ids: tuple[int, ...] = Field(min_length=1, max_length=8)
    lease_epoch: int = Field(ge=1)
    container_name: str
    executable: Literal[False] = False
    created_at: datetime
    content_hash: Sha256 | None = None
    signature: str | None = None


class ExecutionPlan(StrictModel):
    schema_version: Literal["v1"] = "v1"
    key_id: str
    plan_id: ID
    assignment_id: ID
    execution_id: ID
    task_id: ID
    task_content_hash: Sha256
    dispatch_generation: int = Field(ge=1)
    worker_id: str
    unit_id: ID
    gpu_ids: tuple[int, ...] = Field(min_length=1, max_length=8)
    lease_epoch: int = Field(ge=1)
    container_name: str
    submitter_username: str
    container_user: str
    image_digest: ImageDigest
    environment: tuple[tuple[str, str], ...] = ()
    created_at: datetime
    content_hash: Sha256 | None = None
    signature: str | None = None


class Proposal(StrictModel):
    schema_version: Literal["v1"] = "v1"
    proposal_id: ID
    username: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    state: ProposalState
    current_revision_id: ID | None = None
    current_facts_id: ID | None = None
    processor_rounds: int = Field(ge=0, le=8)
    review_count: int = Field(ge=0, le=4)
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    submitter_deadline: datetime | None = None


class IdempotencyRecord(StrictModel):
    schema_version: Literal["v1"] = "v1"
    username: str
    operation: str
    key: str
    request_hash: Sha256
    object_id: ID
    response: dict[str, Any]


class Event(StrictModel):
    schema_version: Literal["v1"] = "v1"
    event_id: ID
    object_type: str
    object_id: ID
    sequence: int = Field(ge=1)
    event_type: str
    actor: str
    request_id: str
    timestamp: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ProtocolEnvelope(StrictModel):
    schema_version: Literal["v1"] = "v1"
    message_id: ID
    sequence: int = Field(ge=1)
    message_type: str
    assignment_id: ID | None = None
    dispatch_generation: int | None = Field(default=None, ge=1)
    lease_epoch: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
