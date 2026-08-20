"""Strict hy-smi sampling and DRIFTED recovery."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

from agent_scheduler.domain.models import GpuSnapshot, GpuState, utc_now

_ROW = re.compile(r"^(?P<id>[0-7])\s+\S+\s+\S+\s+\S+\s+\S+\s+(?P<vram>\d+(?:\.\d+)?)%\s+")


class GpuSamplingError(RuntimeError):
    pass


@dataclass
class HySmiSampler:
    threshold: float
    executable: str = "hy-smi"
    _low_streak: dict[int, int] = field(default_factory=dict)
    _states: dict[int, GpuState] = field(default_factory=dict)

    def sample(self) -> tuple[GpuSnapshot, ...]:
        try:
            result = subprocess.run(
                [self.executable],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GpuSamplingError("hy-smi invocation failed") from exc
        if result.returncode != 0:
            raise GpuSamplingError(result.stderr.strip() or "hy-smi returned nonzero")
        now = utc_now()
        snapshots: list[GpuSnapshot] = []
        seen: set[int] = set()
        for raw_line in result.stdout.splitlines():
            match = _ROW.match(raw_line.strip())
            if not match:
                continue
            gpu_id = int(match.group("id"))
            vram = float(match.group("vram"))
            seen.add(gpu_id)
            previous = self._states.get(gpu_id, GpuState.UNKNOWN)
            if vram >= self.threshold:
                state = GpuState.DRIFTED
                self._low_streak[gpu_id] = 0
            elif previous is GpuState.DRIFTED:
                streak = self._low_streak.get(gpu_id, 0) + 1
                self._low_streak[gpu_id] = streak
                state = GpuState.AVAILABLE if streak >= 3 else GpuState.DRIFTED
            else:
                state = GpuState.AVAILABLE
                self._low_streak[gpu_id] = min(3, self._low_streak.get(gpu_id, 0) + 1)
            self._states[gpu_id] = state
            snapshots.append(
                GpuSnapshot(
                    gpu_id=gpu_id,
                    vram_percent=vram,
                    sampled_at=now,
                    state=state,
                    raw_line=raw_line,
                )
            )
        if seen != set(range(8)):
            raise GpuSamplingError(f"hy-smi did not report exactly GPU IDs 0-7: {sorted(seen)}")
        return tuple(sorted(snapshots, key=lambda snapshot: snapshot.gpu_id))
