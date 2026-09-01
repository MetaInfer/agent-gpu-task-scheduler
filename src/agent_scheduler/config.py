"""Validated process configuration with explicit production/qualification profiles."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

# Admission ceilings per profile. Production stays strict; the qualification profile is an
# explicitly configured, audited exception for the authorized 8x K100_AI node, whose GPUs
# carry resident memory from co-tenant containers.
PRODUCTION_VRAM_CEILING = 2.0
QUALIFICATION_VRAM_CEILING = 97.0


@dataclass(frozen=True)
class Settings:
    state_root: Path
    harness_mode: str
    worker_mode: str
    qualification_profile: bool
    vram_threshold: float
    allowed_users: frozenset[str]
    max_workers: int = 1
    allowed_worker_ids: tuple[str, ...] = ("worker-local-01",)
    worker_api_keys: tuple[tuple[str, str], ...] = ()
    auto_schedule: bool = True

    @classmethod
    def from_file(cls, path: Path) -> Settings:
        """Load Master configuration without consulting process environment variables."""
        value = _json_object(path)
        allowed = {
            "state_root",
            "harness_mode",
            "worker_mode",
            "profile",
            "vram_threshold",
            "allowed_users",
            "max_workers",
            "workers",
            "auto_schedule",
        }
        _reject_unknown(value, allowed, "Master configuration")
        state_root = _path_value(value, "state_root", path.parent)
        harness_mode = value.get("harness_mode", "fake")
        worker_mode = value.get("worker_mode", "remote")
        if harness_mode not in {"fake", "claude"}:
            raise ValueError("harness mode must be fake or claude")
        if worker_mode not in {"fake", "remote"}:
            raise ValueError("worker mode must be fake or remote")
        profile = value.get("profile", "production")
        if profile not in {"production", "qualification"}:
            raise ValueError("profile must be production or qualification")
        qualification = profile == "qualification"
        maximum = QUALIFICATION_VRAM_CEILING if qualification else PRODUCTION_VRAM_CEILING
        threshold = _number(value.get("vram_threshold", maximum), "vram_threshold")
        _validate_threshold(threshold, maximum)
        users_value = value.get("allowed_users", ["zz_chentian"])
        if not isinstance(users_value, list) or not all(
            isinstance(item, str) and item for item in users_value
        ):
            raise ValueError("allowed_users must be a non-empty JSON string array")
        workers_value = value.get("workers")
        if not isinstance(workers_value, dict) or not workers_value:
            raise ValueError("workers must be a non-empty JSON object")
        worker_api_keys: list[tuple[str, str]] = []
        for worker, worker_value in workers_value.items():
            if not isinstance(worker, str) or not worker or not isinstance(worker_value, dict):
                raise ValueError("each Worker entry must map a non-empty ID to an object")
            _reject_unknown(worker_value, {"api_key_file"}, f"Worker {worker}")
            key_path = _path_value(worker_value, "api_key_file", path.parent)
            _validate_secret_file(key_path)
            key = key_path.read_text(encoding="utf-8").strip()
            if len(key) < 32:
                raise ValueError(f"Worker API key for {worker} must contain at least 32 characters")
            worker_api_keys.append((worker, key))
        allowed_worker_ids = tuple(worker for worker, _key in worker_api_keys)
        max_workers = _integer(value.get("max_workers", len(allowed_worker_ids)), "max_workers")
        if max_workers < 1 or max_workers > len(allowed_worker_ids):
            raise ValueError("max workers must be between 1 and the number of allowed Workers")
        auto_schedule = value.get("auto_schedule", True)
        if not isinstance(auto_schedule, bool):
            raise TypeError("auto_schedule must be a JSON boolean")
        return cls(
            state_root=state_root,
            harness_mode=harness_mode,
            worker_mode=worker_mode,
            qualification_profile=qualification,
            vram_threshold=threshold,
            allowed_users=frozenset(users_value),
            max_workers=max_workers,
            allowed_worker_ids=allowed_worker_ids,
            worker_api_keys=tuple(worker_api_keys),
            auto_schedule=auto_schedule,
        )

    @classmethod
    def from_env(cls) -> Settings:
        qualification = os.environ.get("AGENT_SCHEDULER_PROFILE") == "qualification"
        maximum = QUALIFICATION_VRAM_CEILING if qualification else PRODUCTION_VRAM_CEILING
        threshold = float(os.environ.get("AGENT_SCHEDULER_VRAM_THRESHOLD", str(maximum)))
        if not math.isfinite(threshold) or threshold <= 0 or threshold > maximum:
            raise ValueError(
                f"VRAM threshold must be finite and in (0, {maximum}] for this profile"
            )
        harness_mode = os.environ.get("AGENT_SCHEDULER_HARNESS_MODE", "fake")
        worker_mode = os.environ.get("AGENT_SCHEDULER_WORKER_MODE", "remote")
        if harness_mode not in {"fake", "claude"}:
            raise ValueError("harness mode must be fake or claude")
        if worker_mode not in {"fake", "remote"}:
            raise ValueError("worker mode must be fake or remote")
        users = frozenset(
            item.strip()
            for item in os.environ.get("AGENT_SCHEDULER_ALLOWED_USERS", "zz_chentian").split(",")
            if item.strip()
        )
        return cls(
            state_root=Path(
                os.environ.get("AGENT_SCHEDULER_STATE_ROOT", "/public/share/agent-scheduler-mvp")
            ),
            harness_mode=harness_mode,
            worker_mode=worker_mode,
            qualification_profile=qualification,
            vram_threshold=threshold,
            allowed_users=users,
            max_workers=int(os.environ.get("AGENT_SCHEDULER_MAX_WORKERS", "1")),
            auto_schedule=os.environ.get("AGENT_SCHEDULER_AUTO_SCHEDULE", "1") == "1",
        )


@dataclass(frozen=True)
class WorkerSettings:
    state_root: Path
    worker_id: str
    master_uri: str
    api_key: str
    ca_file: Path
    harness_mode: str
    vram_threshold: float

    @classmethod
    def from_file(cls, path: Path) -> WorkerSettings:
        """Load one Worker's local configuration and secret, without environment access."""
        value = _json_object(path)
        _reject_unknown(
            value,
            {
                "state_root",
                "worker_id",
                "master_uri",
                "api_key_file",
                "ca_file",
                "harness_mode",
                "profile",
                "vram_threshold",
            },
            "Worker configuration",
        )
        state_root = _path_value(value, "state_root", path.parent)
        worker_id = value.get("worker_id")
        master_uri = value.get("master_uri")
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id must be a non-empty string")
        if not isinstance(master_uri, str) or not master_uri.startswith("wss://"):
            raise ValueError("master_uri must use wss://")
        api_key_file = _path_value(value, "api_key_file", path.parent)
        _validate_secret_file(api_key_file)
        api_key = api_key_file.read_text(encoding="utf-8").strip()
        if len(api_key) < 32:
            raise ValueError("Worker API key must contain at least 32 characters")
        ca_file = _path_value(value, "ca_file", path.parent)
        if not ca_file.is_file():
            raise FileNotFoundError(f"Worker CA file is missing: {ca_file}")
        harness_mode = value.get("harness_mode", "fake")
        if harness_mode not in {"fake", "claude"}:
            raise ValueError("harness mode must be fake or claude")
        profile = value.get("profile", "production")
        if profile not in {"production", "qualification"}:
            raise ValueError("profile must be production or qualification")
        maximum = (
            QUALIFICATION_VRAM_CEILING if profile == "qualification" else PRODUCTION_VRAM_CEILING
        )
        threshold = _number(value.get("vram_threshold", maximum), "vram_threshold")
        _validate_threshold(threshold, maximum)
        return cls(state_root, worker_id, master_uri, api_key, ca_file, harness_mode, threshold)


def _json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"configuration must be a JSON object: {path}")
    return value


def _reject_unknown(value: dict[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"{label} contains unknown fields: {', '.join(unknown)}")


def _path_value(value: dict[str, object], name: str, base: Path) -> Path:
    raw = value.get(name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{name} must be a non-empty path string")
    result = Path(raw)
    return result if result.is_absolute() else base / result


def _validate_secret_file(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"secret file is missing: {path}")
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"secret file is too permissive: {path}")


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a JSON number")
    return float(value)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a JSON integer")
    return value


def _validate_threshold(threshold: float, maximum: float) -> None:
    if not math.isfinite(threshold) or threshold <= 0 or threshold > maximum:
        raise ValueError(f"VRAM threshold must be finite and in (0, {maximum}] for this profile")
