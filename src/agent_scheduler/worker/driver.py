"""Worker lifecycle Driver and deterministic Fake used by scheduler tests."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_scheduler.domain.models import (
    Command,
    CommandKind,
    ExecutionPlan,
    PrepareManifest,
    Task,
    TaskUnit,
    new_id,
    utc_now,
)
from agent_scheduler.integrity.signing import verify_model
from agent_scheduler.scheduler.core import ExecutionResult
from agent_scheduler.storage.events import EventStore
from agent_scheduler.worker.docker import DockerCLI, DockerError, DockerTimeout

_EXPECTED_IMAGE_ID = "sha256:601d5316da88819c0729b33767b4efad7eb6b50a0e006fffc08624bc647bcbed"
_EXPECTED_MOUNTS = {
    ("/public/share", "/data", True),
    ("/public/home/zz_chentian", "/home", True),
    ("/mnt/nvme1/models", "/models", False),
    ("/opt/hyhal", "/opt/hyhal", False),
}
_EXPECTED_DEVICES = {
    ("/dev/kfd", "/dev/kfd"),
    ("/dev/mkfd", "/dev/mkfd"),
    ("/dev/dri", "/dev/dri"),
}


class DriverError(RuntimeError):
    pass


@dataclass
class FakeWorkerDriver:
    """A side-effect-free Driver for contract and state-machine tests."""

    exit_code: int = 0
    logs_present: bool = True
    outputs_present: bool = True
    cleanup_ok: bool = True
    prepare_ok: bool = True
    plan_ack_ok: bool = True
    prepared: list[str] = field(default_factory=list)
    executed: list[str] = field(default_factory=list)

    def prepare(self, manifest: PrepareManifest, task: Task) -> bool:
        self.prepared.append(manifest.manifest_id)
        return self.prepare_ok

    def acknowledge_plan(self, plan: ExecutionPlan, task: Task) -> bool:
        return self.plan_ack_ok

    def execute(self, plan: ExecutionPlan, task: Task) -> ExecutionResult:
        self.executed.append(plan.plan_id)
        return ExecutionResult(
            self.exit_code,
            self.logs_present,
            self.outputs_present,
            self.cleanup_ok,
            failure_reason="FAKE_RUN_NONZERO" if self.exit_code else None,
        )

    def cancel(self, plan: ExecutionPlan, task: Task) -> bool:
        return self.cleanup_ok

    def query_status(
        self,
        assignment_id: str,
        dispatch_generation: int,
        lease_epoch: int,
        task: Task,
    ) -> str:
        return "PREPARED" if self.prepared else "NOT_REGISTERED"

    def reconcile(self, plan: ExecutionPlan, task: Task) -> bool:
        return self.cleanup_ok


@dataclass
class DockerWorkerDriver:
    docker: DockerCLI
    events: EventStore
    verification_key: Ed25519PublicKey
    key_id: str
    worker_id: str = "worker-local-01"
    _prepared: dict[str, PrepareManifest] = field(default_factory=dict, init=False)
    _plans: dict[str, ExecutionPlan] = field(default_factory=dict, init=False)
    _epoch_high_water: dict[str, int] = field(default_factory=dict, init=False)
    _resource_owner: dict[str, str] = field(default_factory=dict, init=False)
    _state_path: Path = field(init=False)

    def __post_init__(self) -> None:
        directory = self.events.root / "worker-inbox" / self.worker_id
        directory.mkdir(parents=True, exist_ok=True)
        self._state_path = directory / "driver-state.json"
        if not self._state_path.is_file():
            return
        value = json.loads(self._state_path.read_text(encoding="utf-8"))
        self._epoch_high_water = {
            str(key): int(epoch) for key, epoch in value.get("epoch_high_water", {}).items()
        }
        self._resource_owner = {
            str(key): str(owner) for key, owner in value.get("resource_owner", {}).items()
        }
        self._prepared = {
            assignment_id: PrepareManifest.model_validate_json(json.dumps(manifest))
            for assignment_id, manifest in value.get("prepared", {}).items()
        }
        self._plans = {
            assignment_id: ExecutionPlan.model_validate_json(json.dumps(plan))
            for assignment_id, plan in value.get("plans", {}).items()
        }

    def _persist_state(self) -> None:
        temporary = self._state_path.with_suffix(".tmp")
        payload = {
            "epoch_high_water": self._epoch_high_water,
            "resource_owner": self._resource_owner,
            "prepared": {
                key: value.model_dump(mode="json") for key, value in self._prepared.items()
            },
            "plans": {key: value.model_dump(mode="json") for key, value in self._plans.items()},
        }
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._state_path)

    def prepare(self, manifest: PrepareManifest, task: Task) -> bool:
        try:
            self._verify_signed(manifest)
            self._verify_signed(task)
            if manifest.worker_id != self.worker_id or manifest.task_id != task.task_id:
                raise DriverError("PrepareManifest is not bound to this Worker/Task")
            unit = _unit_for_container(task, manifest.container_name)
            self._validate_container(unit, require_stopped=True)
            resources = [f"gpu:{gpu_id}" for gpu_id in manifest.gpu_ids] + [
                f"container:{manifest.container_name}"
            ]
            if any(
                manifest.lease_epoch < self._epoch_high_water.get(resource, 0)
                for resource in resources
            ):
                raise DriverError("STALE_LEASE_EPOCH")
            for resource in resources:
                self._epoch_high_water[resource] = manifest.lease_epoch
                self._resource_owner[resource] = manifest.assignment_id
            self._prepared[manifest.assignment_id] = manifest
            self._persist_state()
            self._audit(
                task,
                "PREPARED",
                {"manifest_id": manifest.manifest_id, "gpu_ids": list(manifest.gpu_ids)},
            )
            return True
        except (DriverError, DockerError):
            return False

    def acknowledge_plan(self, plan: ExecutionPlan, task: Task) -> bool:
        try:
            self._verify_signed(task)
            self._verify_signed(plan)
            manifest = self._prepared[plan.assignment_id]
            if (
                plan.task_id != task.task_id
                or plan.task_content_hash != task.content_hash
                or plan.execution_id != manifest.execution_id
                or plan.dispatch_generation != manifest.dispatch_generation
                or plan.lease_epoch != manifest.lease_epoch
                or plan.worker_id != manifest.worker_id
                or plan.gpu_ids != manifest.gpu_ids
                or plan.container_name != manifest.container_name
            ):
                raise DriverError("ExecutionPlan does not match PrepareManifest/Task")
            self._plans[plan.assignment_id] = plan
            self._persist_state()
            self._audit(task, "PLAN_ACK", {"plan_id": plan.plan_id})
            return True
        except (DriverError, KeyError):
            return False

    def execute(self, plan: ExecutionPlan, task: Task) -> ExecutionResult:
        unit = next(unit for unit in task.units if unit.unit_id == plan.unit_id)
        if self._plans.get(plan.assignment_id) != plan or not self._is_current_plan(plan):
            return ExecutionResult(1, False, False, True, failure_reason="PLAN_NOT_CURRENT")
        start_attempted = False
        forced = False
        failure_reason: str | None = None
        run_exit = 1
        deadline = time.monotonic() + unit.timeout_seconds

        def remaining(limit: int) -> int:
            seconds = int(deadline - time.monotonic())
            if seconds <= 0:
                raise DockerTimeout("Task total deadline expired")
            return min(limit, seconds)

        try:
            self._verify_signed(task)
            self._verify_signed(plan)
            self._validate_container(unit, require_stopped=True)
            self._audit(
                task,
                "DOCKER_INSPECT_PRE",
                {
                    "plan_id": plan.plan_id,
                    "argv": ["docker", "inspect", plan.container_name],
                    "postcondition": "exists=true,running=false,baseline=matched",
                },
            )
            self._audit(
                task,
                "TASK_VERIFIED_PRE_DOCKER",
                {
                    "plan_id": plan.plan_id,
                    "task_content_hash": task.content_hash or "",
                    "lease_epoch": plan.lease_epoch,
                    "dispatch_generation": plan.dispatch_generation,
                },
            )
            start_attempted = True
            self.docker.start(plan.container_name)
            self._audit(
                task,
                "DOCKER_START_CONFIRMED",
                {
                    "plan_id": plan.plan_id,
                    "argv": ["docker", "start", plan.container_name],
                    "postcondition": "running=true",
                },
            )
            setup_exit = self._run_commands(plan, task, "setup", unit.setup, timeout=remaining(120))
            if setup_exit != 0:
                failure_reason = "SETUP_NONZERO"
                self._run_commands(plan, task, "teardown", unit.teardown, timeout=remaining(120))
            else:
                run_exit = self._run_commands(plan, task, "run", unit.run, timeout=remaining(300))
                if run_exit != 0:
                    failure_reason = "RUN_NONZERO"
                self._run_commands(plan, task, "teardown", unit.teardown, timeout=remaining(120))
        except DockerTimeout:
            forced = True
            failure_reason = "RUN_TIMEOUT"
            run_exit = 124
        except (DockerError, DriverError) as exc:
            forced = True
            failure_reason = str(exc)
            run_exit = 1
        cleanup_ok, warning = (
            self._cleanup(plan.container_name) if start_attempted else (True, None)
        )
        if start_attempted:
            self._audit(
                task,
                "DOCKER_CLEANUP_INSPECTED",
                {
                    "plan_id": plan.plan_id,
                    "argv": [
                        ["docker", "stop", "--time", "30", plan.container_name],
                        ["docker", "kill", plan.container_name],
                        ["docker", "inspect", plan.container_name],
                    ],
                    "cleanup_ok": cleanup_ok,
                    "warning": warning,
                },
            )
        logs_present = all(Path(path).is_file() for path in unit.required_logs)
        outputs_present = all(Path(path).is_file() for path in unit.required_outputs)
        self._audit(
            task,
            "EXECUTION_FINISHED",
            {
                "plan_id": plan.plan_id,
                "exit_code": run_exit,
                "forced": forced,
                "logs_present": logs_present,
                "outputs_present": outputs_present,
                "cleanup_ok": cleanup_ok,
                "warning": warning,
                "failure_reason": failure_reason,
            },
        )
        return ExecutionResult(
            run_exit,
            logs_present,
            outputs_present,
            cleanup_ok,
            warning=warning,
            failure_reason=failure_reason,
        )

    def _is_current_plan(self, plan: ExecutionPlan) -> bool:
        resources = [f"gpu:{gpu_id}" for gpu_id in plan.gpu_ids] + [
            f"container:{plan.container_name}"
        ]
        return all(
            self._epoch_high_water.get(resource) == plan.lease_epoch
            and self._resource_owner.get(resource) == plan.assignment_id
            for resource in resources
        )

    def query_status(
        self,
        assignment_id: str,
        dispatch_generation: int,
        lease_epoch: int,
        task: Task,
    ) -> str:
        plan = self._plans.get(assignment_id)
        manifest = self._prepared.get(assignment_id)
        if plan is not None:
            if (
                plan.dispatch_generation != dispatch_generation
                or plan.lease_epoch != lease_epoch
                or not self._is_current_plan(plan)
            ):
                return "STALE"
            completed = (
                self.events.root
                / "worker-inbox"
                / self.worker_id
                / task.execution_id
                / "driver-events.jsonl"
            )
            if completed.is_file() and '"event_type":"EXECUTION_FINISHED"' in completed.read_text(
                encoding="utf-8"
            ):
                return "FINISHED"
            try:
                if self.docker.inspect(plan.container_name).running:
                    return "RUNNING"
            except DockerError:
                return "UNKNOWN"
            return "PLAN_ACK"
        if manifest is not None:
            if (
                manifest.dispatch_generation != dispatch_generation
                or manifest.lease_epoch != lease_epoch
            ):
                return "STALE"
            return "PREPARED"
        return "NOT_REGISTERED"

    def cancel(self, plan: ExecutionPlan, task: Task) -> bool:
        try:
            self._verify_signed(task)
            self._verify_signed(plan)
            if self._plans.get(plan.assignment_id) != plan or not self._is_current_plan(plan):
                raise DriverError("cancel references a stale assignment")
            cleanup_ok, _warning = self._cleanup(plan.container_name)
            self._audit(task, "CANCELLED_BY_MASTER", {"plan_id": plan.plan_id})
            return cleanup_ok
        except DriverError:
            return False

    def reconcile(self, plan: ExecutionPlan, task: Task) -> bool:
        try:
            self._verify_signed(task)
            self._verify_signed(plan)
            if self._plans.get(plan.assignment_id) != plan or not self._is_current_plan(plan):
                raise DriverError("reconcile references a stale assignment")
            inspection = self.docker.inspect(plan.container_name)
            return inspection.exists and not inspection.running
        except (DriverError, DockerError):
            return False

    def _run_commands(
        self,
        plan: ExecutionPlan,
        task: Task,
        phase: str,
        commands: tuple[Command, ...],
        *,
        timeout: int,
    ) -> int:
        framework_dir = (
            self.events.root / "framework-logs" / task.task_id / plan.unit_id / task.execution_id
        )
        framework_dir.mkdir(parents=True, exist_ok=True)
        environment = dict(plan.environment)
        for index, command in enumerate(commands, 1):
            if command.kind is CommandKind.INLINE_BASH:
                result = self.docker.exec_attached(
                    plan.container_name,
                    ["/bin/bash", "-s", "--", *command.argv],
                    environment=environment,
                    stdin=command.script,
                    timeout=timeout,
                )
            else:
                assert command.container_path and command.sha256
                digest_result = self.docker.exec_attached(
                    plan.container_name,
                    ["sha256sum", command.container_path],
                    environment=environment,
                    timeout=30,
                )
                actual = (
                    digest_result.stdout.split(maxsplit=1)[0]
                    if digest_result.returncode == 0
                    else ""
                )
                if not secrets_compare(actual, command.sha256):
                    raise DriverError("SCRIPT_HASH_MISMATCH")
                result = self.docker.exec_attached(
                    plan.container_name,
                    ["/bin/bash", command.container_path, *command.argv],
                    environment=environment,
                    timeout=timeout,
                )
            evidence = {
                "schema_version": "v1",
                "phase": phase,
                "index": index,
                "argv": _redacted_argv(command),
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timestamp": utc_now().isoformat(),
            }
            (framework_dir / f"{phase}-{index:03d}.json").write_text(
                json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            if result.returncode != 0:
                return result.returncode
        return 0

    def _validate_container(self, unit: TaskUnit, *, require_stopped: bool) -> None:
        if (
            unit.worker_id,
            unit.container_name,
            unit.submitter_username,
        ) != (self.worker_id, "fh-sglang-deepseek-v4-flash", "zz_chentian"):
            raise DriverError("container allowlist triple mismatch")
        inspection = self.docker.inspect(unit.container_name)
        if not inspection.exists:
            raise DriverError("reuse container does not exist")
        if require_stopped and inspection.running:
            raise DriverError("reuse container must be stopped before assignment")
        if inspection.image_id != _EXPECTED_IMAGE_ID:
            raise DriverError("container image ID differs from frozen baseline")
        if unit.image_digest not in self.docker.image_repo_digests(inspection.image_id or ""):
            raise DriverError("container repository digest differs from Task")
        if (
            inspection.user not in {"root", "0", ""}
            or inspection.working_dir != "/workspace"
            or not inspection.privileged
            or inspection.network_mode != "host"
            or inspection.ipc_mode != "host"
            or inspection.shm_size != 16 * 1024**3
        ):
            raise DriverError("container privilege/namespace baseline mismatch")
        actual_mounts = {
            (mount.source, mount.destination, mount.read_write) for mount in inspection.mounts
        }
        if actual_mounts != _EXPECTED_MOUNTS:
            raise DriverError("container mount baseline mismatch")
        if not _EXPECTED_DEVICES.issubset(set(inspection.devices)):
            raise DriverError("container device baseline mismatch")
        if any(mount.destination == "/var/run/docker.sock" for mount in inspection.mounts):
            raise DriverError("Docker socket mount is forbidden")

    def _cleanup(self, name: str) -> tuple[bool, str | None]:
        try:
            inspection, warning = self.docker.stop(name)
            return inspection.exists and not inspection.running, warning
        except DockerError:
            try:
                inspection = self.docker.inspect(name)
                return inspection.exists and not inspection.running, None
            except DockerError:
                return False, None

    def _verify_signed(self, value: Task | PrepareManifest | ExecutionPlan) -> None:
        if value.key_id != self.key_id or not verify_model(value, self.verification_key):
            raise DriverError("signature or key_id is invalid")

    def _audit(self, task: Task, event_type: str, payload: dict[str, object]) -> None:
        inbox = self.events.root / "worker-inbox" / self.worker_id / task.execution_id
        inbox.mkdir(parents=True, exist_ok=True)
        record = (
            json.dumps(
                {
                    "event_id": new_id("worker_evt"),
                    "event_type": event_type,
                    "timestamp": utc_now().isoformat(),
                    "task_id": task.task_id,
                    "payload": payload,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        fd = os.open(inbox / "driver-events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            if os.write(fd, record) != len(record):
                raise OSError("partial Worker inbox append")
            os.fsync(fd)
        finally:
            os.close(fd)


def _unit_for_container(task: Task, container_name: str) -> TaskUnit:
    matches = [unit for unit in task.units if unit.container_name == container_name]
    if len(matches) != 1:
        raise DriverError("PrepareManifest container does not map to one TaskUnit")
    return matches[0]


def secrets_compare(actual: str, expected: str) -> bool:
    import secrets

    return secrets.compare_digest(actual, expected)


def _redacted_argv(command: Command) -> list[str]:
    if command.kind is CommandKind.INLINE_BASH:
        return ["/bin/bash", "-s", "--", *command.argv]
    return ["/bin/bash", command.container_path or "", *command.argv]
