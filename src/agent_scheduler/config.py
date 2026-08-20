"""Validated process configuration with explicit production/qualification profiles."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    state_root: Path
    harness_mode: str
    worker_mode: str
    qualification_profile: bool
    vram_threshold: float
    allowed_users: frozenset[str]
    max_workers: int = 1
    worker_id: str = "worker-local-01"
    auto_schedule: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        qualification = os.environ.get("AGENT_SCHEDULER_PROFILE") == "qualification"
        default_threshold = "90" if qualification else "2"
        threshold = float(os.environ.get("AGENT_SCHEDULER_VRAM_THRESHOLD", default_threshold))
        maximum = 90.0 if qualification else 2.0
        if not math.isfinite(threshold) or threshold <= 0 or threshold > maximum:
            raise ValueError(
                f"VRAM threshold must be finite and in (0, {maximum}] for this profile"
            )
        harness_mode = os.environ.get("AGENT_SCHEDULER_HARNESS_MODE", "fake")
        worker_mode = os.environ.get("AGENT_SCHEDULER_WORKER_MODE", "remote")
        if harness_mode not in {"fake", "claude"}:
            raise ValueError("harness mode must be fake or claude")
        if worker_mode not in {"fake", "local", "remote"}:
            raise ValueError("worker mode must be fake, local, or remote")
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
