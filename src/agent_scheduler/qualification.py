"""Real Claude + WSS + Docker + GPU qualification orchestration and evidence checks."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal, cast

import httpx

from agent_scheduler.adapters.submitter import build_submitter_invocation
from agent_scheduler.config import QUALIFICATION_VRAM_CEILING
from agent_scheduler.domain.models import (
    ExecutionPlan,
    PrepareManifest,
    Proposal,
    ProposalFacts,
    ResourceLease,
    Review,
    StrictModel,
    Task,
    TaskStatus,
    WorkerSnapshot,
    new_id,
    strict_from_json_value,
    utc_now,
)
from agent_scheduler.integrity import canonical_bytes, verify_model
from agent_scheduler.runtime import RuntimeIdentity
from agent_scheduler.storage import EventStore, StoreCorruptionError
from agent_scheduler.worker.docker import DockerCLI, DockerError

_LAUNCHER_PATH = "/data/fh/agent-gpu-task-scheduler/scripts/run_torch_collective_smoke.sh"
_LAUNCHER_SHA256 = "c1cf6dee074e03c026dd7272e358d7b15c65b6ff3b6ae1c1f71e7efae341de0c"
_TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "CLEANUP_FAILED"}


class QualificationItem(StrictModel):
    card_count: Literal[1, 2, 4, 8]
    proposal_id: str
    task_id: str
    state: str


class QualificationResult(StrictModel):
    schema_version: Literal["v1"] = "v1"
    run_id: str
    status: Literal["COMPLETED", "BLOCKED_QUALIFICATION"]
    items: tuple[QualificationItem, ...]
    reason: str | None = None


def run_submitter_agent(
    *,
    project_root: Path,
    state_root: Path,
    base_url: str,
    tls_certificate: Path,
    timeout_seconds: int = 45 * 60,
    executable: str | None = None,
    harness: str = "claude",
) -> QualificationResult:
    run_id = new_id("qual")
    store: EventStore | None = None
    invocation_id = new_id("harness")
    started = utc_now()
    command: list[str] = []
    audit: dict[str, object] = {
        "schema_version": "v1",
        "invocation_id": invocation_id,
        "run_id": run_id,
        "role": "submitter",
        "harness": harness,
        "started_at": started.isoformat(),
    }
    try:
        if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            return _blocked(
                (),
                "ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is required for real Claude roles",
                run_id,
            )
        store = EventStore(state_root)
        gate_failure = _run_local_gates(project_root, store, run_id)
        if gate_failure:
            return _blocked((), gate_failure, run_id)
        profile = _master_profile(base_url, tls_certificate)
        if profile != {
            "qualification": True,
            "harness_mode": "claude",
            "worker_mode": "remote",
        }:
            return _blocked((), f"Master is not in real qualification mode: {profile}", run_id)
        store.write_immutable(
            "qualification-runs",
            run_id,
            {
                "schema_version": "v1",
                "run_id": run_id,
                "started_at": started.isoformat(),
                "profile": profile,
            },
        )
        uv_path = shutil.which("uv")
        if uv_path is None:
            return _blocked((), "uv executable not found", run_id)
        run_dir = state_root / "qualification" / run_id
        invocation = build_submitter_invocation(
            harness,
            output_dir=run_dir,
            project_root=project_root,
            state_root=state_root,
            base_url=base_url,
            username="zz_chentian",
            uv_path=Path(uv_path).resolve(),
            tls_certificate=tls_certificate,
            executable=executable,
            run_id=run_id,
        )
        command = list(invocation.argv)
        prompt = invocation.prompt
        env = invocation.env
        cli_version = _harness_version(command[0], env)
        last_reason = "Submitter exhausted retry attempts"
        for attempt in range(1, 5):
            attempt_id = invocation_id if attempt == 1 else new_id("harness")
            attempt_audit = {
                **audit,
                "invocation_id": attempt_id,
                "attempt": attempt,
                "argv": command,
                "cli_version": cli_version,
                "cwd": str(project_root),
            }
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    check=False,
                    timeout=timeout_seconds,
                    cwd=project_root,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                attempt_audit.update(
                    {"ended_at": utc_now().isoformat(), "error": "submitter_timeout"}
                )
                store.write_immutable("harness", attempt_id, attempt_audit)
                last_reason = "Submitter qualification attempt timed out"
                if attempt < 4:
                    continue
                return _blocked((), last_reason, run_id)
            attempt_audit.update(
                {
                    "ended_at": utc_now().isoformat(),
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            store.write_immutable("harness", attempt_id, attempt_audit)
            if completed.returncode != 0:
                reconstructed = reconstruct_qualification_result(store, run_id)
                if reconstructed.status == "COMPLETED":
                    return reconstructed
                last_reason = (
                    f"Submitter {harness} exited with code {completed.returncode}: "
                    f"{reconstructed.reason}"
                )
                if _retryable_submitter_failure(completed.stderr) and attempt < 4:
                    continue
                return _blocked(reconstructed.items, last_reason, run_id)
            return reconstruct_qualification_result(store, run_id)
        return _blocked((), last_reason, run_id)
    except (
        OSError,
        DockerError,
        StoreCorruptionError,
        httpx.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        audit.update(
            {
                "ended_at": utc_now().isoformat(),
                "argv": command,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        if store is not None and not store.immutable_exists("harness", invocation_id):
            store.write_immutable("harness", invocation_id, audit)
        return _blocked((), f"qualification launch failed: {type(exc).__name__}: {exc}", run_id)


def verify_qualification(
    result: QualificationResult,
    *,
    state_root: Path,
    identity: RuntimeIdentity,
    harness: str = "claude",
    docker: DockerCLI | None = None,
) -> QualificationResult:
    """Fail closed unless the complete current-run evidence graph validates."""
    if result.status != "COMPLETED":
        return result
    try:
        store = EventStore(state_root)
        store.validate_all_events()
        run_record = store.read_immutable("qualification-runs", result.run_id)
        if run_record.get("profile") != {
            "qualification": True,
            "harness_mode": "claude",
            "worker_mode": "remote",
        }:
            return _blocked(result.items, "qualification run profile is invalid", result.run_id)
        gate_records = [
            value
            for value in store.iter_immutable("qualification-gates")
            if value.get("run_id") == result.run_id and value.get("passed") is True
        ]
        if len(gate_records) != 1:
            return _blocked(
                result.items, "local quality gates are not bound and passing", result.run_id
            )
        gate_results = gate_records[0].get("results")
        expected_gate_markers = {"pytest", "ruff", "mypy"}
        if (
            not isinstance(gate_results, list)
            or len(gate_results) != 3
            or any(
                not isinstance(gate, dict)
                or gate.get("exit_code") != 0
                or not isinstance(gate.get("argv"), list)
                for gate in gate_results
            )
            or {
                marker
                for marker in expected_gate_markers
                if any(
                    isinstance(gate, dict)
                    and isinstance(gate.get("argv"), list)
                    and marker in " ".join(map(str, gate["argv"]))
                    for gate in gate_results
                )
            }
            != expected_gate_markers
        ):
            return _blocked(result.items, "local gate command evidence is invalid", result.run_id)
        expected_cards = {1, 2, 4, 8}
        if {item.card_count for item in result.items} != expected_cards or len(result.items) != 4:
            return _blocked(
                result.items, "result does not contain exactly 1/2/4/8 cards", result.run_id
            )
        all_harness = list(store.iter_immutable("harness"))
        submitter = [
            item
            for item in all_harness
            if item.get("run_id") == result.run_id
            and item.get("role") == "submitter"
            and item.get("exit_code") == 0
            # Evidence recorded before the field existed came from Claude Code.
            and item.get("harness", "claude") == harness
        ]
        if len(submitter) != 1:
            return _blocked(
                result.items,
                f"current {harness} Submitter evidence is incomplete",
                result.run_id,
            )
        for item in result.items:
            failure = _verify_item(item, result.run_id, store, identity, all_harness, harness)
            if failure:
                return _blocked(result.items, failure, result.run_id)
        leases = _active_leases(store)
        execution_ids = {
            strict_from_json_value(Task, store.read_immutable("tasks", item.task_id)).execution_id
            for item in result.items
        }
        if any(lease.execution_id in execution_ids for lease in leases.values()):
            return _blocked(result.items, "qualification execution retains a Lease", result.run_id)
        inspection = (docker or DockerCLI()).inspect("fh-sglang-deepseek-v4-flash")
        if not inspection.exists or inspection.running:
            return _blocked(result.items, "reuse container is not confirmed stopped", result.run_id)
        return result
    except (
        OSError,
        DockerError,
        StoreCorruptionError,
        httpx.HTTPError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        return _blocked(
            result.items,
            f"qualification evidence failed closed: {type(exc).__name__}: {exc}",
            result.run_id,
        )


def _verify_item(
    item: QualificationItem,
    run_id: str,
    store: EventStore,
    identity: RuntimeIdentity,
    harness_records: list[dict[str, Any]],
    harness: str,
) -> str | None:
    submitter_records = [
        value
        for value in harness_records
        if value.get("role") == "submitter"
        and value.get("run_id") == run_id
        and value.get("harness", "claude") == harness
    ]
    if (
        len(submitter_records) != 1
        or not _record_mentions(submitter_records[0], item.proposal_id)
        or not _record_mentions(submitter_records[0], item.task_id)
    ):
        return f"Submitter evidence is not bound to current item: {item.task_id}"
    task = strict_from_json_value(Task, store.read_immutable("tasks", item.task_id))
    if task.proposal_id != item.proposal_id or not verify_model(task, identity.signing_public_key):
        return f"Task/Proposal/signature binding invalid: {item.task_id}"
    canonical_path = store.immutable_root / "tasks" / f"{task.task_id}.canonical.json"
    if canonical_path.read_bytes() != canonical_bytes(task):
        return f"canonical Task sidecar invalid: {task.task_id}"
    proposal_data = store.read_snapshot("proposals", item.proposal_id)
    if proposal_data is None:
        return f"Proposal snapshot missing: {item.proposal_id}"
    proposal = strict_from_json_value(Proposal, proposal_data)
    revision_data = store.read_immutable("revisions", task.revision_id)
    revision_markdown = revision_data.get("markdown")
    if (
        proposal.state.value != "COMPILED"
        or not isinstance(revision_markdown, str)
        or f"Qualification Run: {run_id}" not in revision_markdown
    ):
        return f"Proposal is not bound to current qualification run: {item.proposal_id}"
    facts = strict_from_json_value(ProposalFacts, store.read_immutable("facts", task.facts_id))
    review = strict_from_json_value(Review, store.read_immutable("reviews", task.review_id))
    unit = task.units[0] if len(task.units) == 1 else None
    if (
        unit is None
        or unit.required_gpu_count != item.card_count
        or unit.run[0].container_path != _LAUNCHER_PATH
        or unit.run[0].sha256 != _LAUNCHER_SHA256
        or facts.revision_id != task.revision_id
        or review.revision_id != task.revision_id
        or review.decision.value != "APPROVE"
    ):
        return f"frozen workload/Facts/Review invalid: {task.task_id}"
    status = _latest_task_status(store, task.task_id)
    if status is None:
        return f"Task status history missing: {task.task_id}"
    if status.state.value != "COMPLETED" or item.state != "COMPLETED":
        return f"Task is not COMPLETED: {task.task_id}"
    lease_actions = {
        value.get("action")
        for value in store.iter_immutable("lease-history")
        if isinstance(value.get("lease"), dict)
        and value["lease"].get("execution_id") == task.execution_id
    }
    if not {"COMMITTED", "RELEASED"}.issubset(lease_actions):
        return f"Lease commit/release evidence incomplete: {task.task_id}"
    plans = [
        strict_from_json_value(ExecutionPlan, value)
        for value in store.iter_immutable("plans")
        if value.get("task_id") == task.task_id
    ]
    if len(plans) != 1:
        return f"Task does not have exactly one immutable Plan: {task.task_id}"
    plan = plans[0]
    if (
        not verify_model(plan, identity.signing_public_key)
        or plan.key_id != identity.key_id
        or plan.task_content_hash != task.content_hash
        or plan.execution_id != task.execution_id
        or len(plan.gpu_ids) != item.card_count
    ):
        return f"Execution Plan binding/signature invalid: {task.task_id}"
    manifests = [
        strict_from_json_value(PrepareManifest, value)
        for value in store.iter_immutable("manifests")
        if value.get("assignment_id") == plan.assignment_id
    ]
    if (
        len(manifests) != 1
        or not verify_model(manifests[0], identity.signing_public_key)
        or manifests[0].lease_epoch != plan.lease_epoch
        or manifests[0].gpu_ids != plan.gpu_ids
        or manifests[0].dispatch_generation != plan.dispatch_generation
    ):
        return f"PrepareManifest binding/signature invalid: {task.task_id}"
    samples = [
        strict_from_json_value(WorkerSnapshot, value)
        for value in store.iter_immutable("worker-samples")
        if value.get("worker_id") == plan.worker_id
    ]
    fresh_sample = next(
        (
            sample
            for sample in reversed(samples)
            if sample.last_heartbeat_at <= plan.created_at
            and (plan.created_at - sample.last_heartbeat_at).total_seconds() <= 30
            and all(
                gpu.gpu_id in plan.gpu_ids
                and gpu.state.value == "AVAILABLE"
                and gpu.vram_percent < QUALIFICATION_VRAM_CEILING
                and bool(gpu.raw_line)
                and gpu.sampled_at <= plan.created_at
                and (plan.created_at - gpu.sampled_at).total_seconds() <= 30
                for gpu in sample.gpus
                if gpu.gpu_id in plan.gpu_ids
            )
            and {gpu.gpu_id for gpu in sample.gpus if gpu.gpu_id in plan.gpu_ids}
            == set(plan.gpu_ids)
        ),
        None,
    )
    if fresh_sample is None:
        return f"fresh raw hy-smi evidence missing: {task.task_id}"
    worker_object_id = f"worker_{hashlib.sha256(plan.worker_id.encode()).hexdigest()[:32]}"
    protocol_events = {
        event.event_type
        for event in store.list_events("workers", worker_object_id)
        if event.payload.get("assignment_id") == plan.assignment_id
    }
    required_protocol = {
        "SEND_PREPARE",
        "MESSAGE_PREPARE_RESULT",
        "SEND_PLAN",
        "MESSAGE_PLAN_RESULT",
        "SEND_EXECUTE",
        "MESSAGE_EXECUTE_RESULT",
    }
    if not required_protocol.issubset(protocol_events):
        return f"current WSS protocol evidence incomplete: {task.task_id}"
    event_types = {event.event_type for event in store.list_events("tasks", task.task_id)}
    required = {"PREPARED", "PLAN_ACK", "RUNNING", "COMPLETED"}
    if not required.issubset(event_types):
        return f"barrier evidence incomplete: {task.task_id}"
    worker_events = list(store.iter_immutable("worker-evidence"))
    evidence = [value for value in worker_events if value.get("task_id") == task.task_id]
    if not any(value.get("event_type") == "TASK_VERIFIED_PRE_DOCKER" for value in evidence):
        return f"pre-Docker verification evidence missing: {task.task_id}"
    required_worker_evidence = {
        "TASK_VERIFIED_PRE_DOCKER",
        "DOCKER_INSPECT_PRE",
        "DOCKER_START_CONFIRMED",
        "DOCKER_CLEANUP_INSPECTED",
        "EXECUTION_FINISHED",
    }
    evidence_types = {value.get("event_type") for value in evidence}
    if not required_worker_evidence.issubset(evidence_types):
        return f"Docker lifecycle evidence missing: {task.task_id}"
    roles = {
        value.get("role")
        for value in harness_records
        if value.get("exit_code") == 0 and _record_mentions(value, task.revision_id)
    }
    if not {"processor", "reviewer"}.issubset(roles):
        return f"Processor/Reviewer evidence is not bound to revision: {task.revision_id}"
    controller = [
        value
        for value in harness_records
        if value.get("role") == "worker-controller"
        and value.get("exit_code") == 0
        and _record_mentions(value, plan.assignment_id)
    ]
    if len(controller) != 1:
        return f"Worker Controller evidence is not bound to assignment: {plan.assignment_id}"
    framework_dir = store.root / "framework-logs" / task.task_id / unit.unit_id / task.execution_id
    run_records = []
    for path in sorted(framework_dir.glob("run-*.json")):
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            run_records.append(value)
    if (
        len(run_records) != 1
        or run_records[0].get("exit_code") != 0
        or run_records[0].get("argv", [None, None])[1] != _LAUNCHER_PATH
    ):
        return f"frozen Docker exec evidence invalid: {task.task_id}"
    for artifact_path_value in unit.required_logs + unit.required_outputs:
        artifact = Path(artifact_path_value)
        if not artifact.is_file() or artifact.stat().st_mtime < task.created_at.timestamp():
            return f"required current-run artifact missing or stale: {artifact_path_value}"
    output_path = Path(unit.required_outputs[0])
    output = json.loads(output_path.read_text(encoding="utf-8"))
    ranks = output.get("ranks")
    expected_value = item.card_count * (item.card_count + 1) / 2
    if (
        output.get("task_id") != task.task_id
        or output.get("unit_id") != unit.unit_id
        or output.get("execution_id") != task.execution_id
        or output.get("plan_id") != plan.plan_id
        or output.get("assignment_id") != plan.assignment_id
        or output.get("lease_epoch") != plan.lease_epoch
        or output.get("gpu_ids") != list(plan.gpu_ids)
        or output.get("world_size") != item.card_count
        or output.get("all_reduce") != "ok"
        or output.get("gemm") != "ok"
        or output.get("rocm_version") is None
        or not isinstance(ranks, list)
        or {rank.get("rank") for rank in ranks if isinstance(rank, dict)}
        != set(range(item.card_count))
        or {rank.get("local_rank") for rank in ranks if isinstance(rank, dict)}
        != set(range(item.card_count))
        or any(
            not isinstance(rank, dict)
            or rank.get("all_reduce_value") != expected_value
            or not isinstance(rank.get("gemm_elapsed_seconds"), (int, float))
            for rank in ranks
        )
    ):
        return f"numerical rank evidence invalid: {task.task_id}"
    return None


def _latest_task_status(store: EventStore, task_id: str) -> TaskStatus | None:
    statuses = [
        strict_from_json_value(TaskStatus, value)
        for value in store.iter_immutable("task-status-history")
        if value.get("task_id") == task_id
    ]
    return max(statuses, key=lambda status: status.updated_at) if statuses else None


def _active_leases(store: EventStore) -> dict[str, ResourceLease]:
    active: dict[str, ResourceLease] = {}
    for value in store.iter_immutable("lease-history"):
        lease = strict_from_json_value(ResourceLease, value["lease"])
        action = value.get("action")
        if action in {"HELD", "COMMITTED"}:
            active[lease.lease_id] = lease
        elif action == "RELEASED":
            active.pop(lease.lease_id, None)
        else:
            raise ValueError("unknown Lease history action")
    return active


def _record_mentions(record: dict[str, Any], identifier: str) -> bool:
    return identifier in json.dumps(record, sort_keys=True, ensure_ascii=False)


def _master_profile(base_url: str, certificate: Path) -> dict[str, object]:
    response = httpx.get(f"{base_url}/health", verify=str(certificate), timeout=10)
    response.raise_for_status()
    value = response.json()
    if not isinstance(value, dict):
        raise TypeError("Master health is not an object")
    return {
        "qualification": value.get("qualification"),
        "harness_mode": value.get("harness_mode"),
        "worker_mode": value.get("worker_mode"),
    }


def _run_local_gates(project_root: Path, store: EventStore, run_id: str) -> str | None:
    uv_path = shutil.which("uv")
    if uv_path is None:
        return "uv executable not found for local gates"
    real_marker_filter = (
        "not real_claude and not real_codex and not real_pi and not real_dsh and not real_gpu"
    )
    commands = (
        [uv_path, "run", "pytest", "-m", real_marker_filter, "-q"],
        [uv_path, "run", "ruff", "check", "."],
        [uv_path, "run", "mypy", "src"],
    )
    results: list[dict[str, object]] = []
    passed = True
    env = os.environ.copy()
    for name in (
        "RUN_REAL_CLAUDE",
        "RUN_REAL_CODEX",
        "RUN_REAL_PI",
        "RUN_REAL_DSH",
        "RUN_REAL_GPU",
        "RUN_FULL_QUALIFICATION",
    ):
        env.pop(name, None)
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                env=env,
                text=True,
                capture_output=True,
                check=False,
                timeout=10 * 60,
            )
            results.append(
                {
                    "argv": command,
                    "exit_code": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                }
            )
            passed = passed and completed.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as exc:
            results.append({"argv": command, "error": f"{type(exc).__name__}: {exc}"})
            passed = False
    store.write_immutable(
        "qualification-gates",
        new_id("gate"),
        {
            "schema_version": "v1",
            "run_id": run_id,
            "passed": passed,
            "timestamp": utc_now().isoformat(),
            "results": results,
        },
    )
    return None if passed else "one or more local quality gates failed"


def _retryable_submitter_failure(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(
        marker in lowered
        for marker in ("rate limit", "429", "timed out", "timeout", "temporarily unavailable")
    )


def _harness_version(executable: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        [executable, "--version"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env=env,
    )
    if result.returncode != 0:
        raise OSError(f"{executable} version check returned nonzero")
    return result.stdout.strip()


def _blocked(items: tuple[QualificationItem, ...], reason: str, run_id: str) -> QualificationResult:
    return QualificationResult(
        run_id=run_id, status="BLOCKED_QUALIFICATION", items=items, reason=reason
    )


_EXPECTED_CARD_COUNTS = (1, 2, 4, 8)


def reconstruct_qualification_result(store: EventStore, run_id: str) -> QualificationResult:
    """Derive the run's outcome from Ground Truth instead of asking the Agent.

    The four harnesses differ most in structured-output support, and the result is
    only a set of pointers that ``verify_qualification`` re-validates anyway. The
    ``Qualification Run: <run_id>`` binding is already required by ``_verify_item``,
    so reuse it as the discovery mechanism rather than adding an output contract
    that the weakest CLI cannot honour.
    """
    marker = f"Qualification Run: {run_id}"
    revision_to_proposal = {
        str(value["revision_id"]): str(value["proposal_id"])
        for value in store.iter_immutable("revisions")
        if isinstance(value.get("markdown"), str)
        and marker in value["markdown"]
        and isinstance(value.get("revision_id"), str)
        and isinstance(value.get("proposal_id"), str)
    }
    compiled = {
        proposal_id
        for proposal_id in set(revision_to_proposal.values())
        if (snapshot := store.read_snapshot("proposals", proposal_id)) is not None
        and snapshot.get("state") == "COMPILED"
    }
    items: list[QualificationItem] = []
    for value in store.iter_immutable("tasks"):
        task = strict_from_json_value(Task, value)
        if task.proposal_id not in compiled or task.revision_id not in revision_to_proposal:
            continue
        if len(task.units) != 1:
            continue
        card_count = task.units[0].required_gpu_count
        if card_count not in _EXPECTED_CARD_COUNTS:
            continue
        status = _latest_task_status(store, task.task_id)
        items.append(
            QualificationItem(
                card_count=cast(Literal[1, 2, 4, 8], card_count),
                proposal_id=task.proposal_id,
                task_id=task.task_id,
                state=status.state.value if status is not None else "UNKNOWN",
            )
        )
    items.sort(key=lambda item: (item.card_count, item.task_id))
    found = {item.card_count for item in items}
    missing = [count for count in _EXPECTED_CARD_COUNTS if count not in found]
    incomplete = [item.task_id for item in items if item.state != "COMPLETED"]
    if missing:
        return _blocked(
            tuple(items),
            f"run produced no COMPLETED Task for card counts {missing}",
            run_id,
        )
    if incomplete:
        return _blocked(
            tuple(items), f"Tasks did not reach COMPLETED: {incomplete}", run_id
        )
    if len(items) != len(_EXPECTED_CARD_COUNTS):
        return _blocked(
            tuple(items),
            f"run produced {len(items)} Tasks for {len(_EXPECTED_CARD_COUNTS)} card counts",
            run_id,
        )
    return QualificationResult(run_id=run_id, status="COMPLETED", items=tuple(items))
