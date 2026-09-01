import subprocess

import pytest

from agent_scheduler.domain.models import GpuState
from agent_scheduler.worker.gpu import GpuSamplingError, HySmiSampler


def output(vram: int, count: int = 8) -> str:
    header = "DCU Temp AvgPwr Perf PwrCap VRAM% DCU% Mode\n"
    rows = "\n".join(
        f"{index} 50.0C 100.0W auto 400.0W {vram}% 0.0% Normal" for index in range(count)
    )
    return header + rows + "\n"


def test_drifted_gpu_requires_three_low_samples(monkeypatch):
    values = iter([output(92), output(80), output(80), output(80)])

    def run(*args, **kwargs):
        return subprocess.CompletedProcess(["hy-smi"], 0, next(values), "")

    monkeypatch.setattr(subprocess, "run", run)
    sampler = HySmiSampler(90)
    assert all(item.state is GpuState.DRIFTED for item in sampler.sample())
    assert all(item.state is GpuState.DRIFTED for item in sampler.sample())
    assert all(item.state is GpuState.DRIFTED for item in sampler.sample())
    assert all(item.state is GpuState.AVAILABLE for item in sampler.sample())


def test_four_gpu_worker_is_accepted(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["hy-smi"], 0, output(1, 4), ""),
    )
    snapshots = HySmiSampler(2).sample()
    assert [item.gpu_id for item in snapshots] == [0, 1, 2, 3]


def test_noncontiguous_gpu_ids_are_rejected(monkeypatch):
    malformed = output(1, 4).replace("2 50.0C", "4 50.0C")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(["hy-smi"], 0, malformed, ""),
    )
    with pytest.raises(GpuSamplingError, match="contiguous"):
        HySmiSampler(2).sample()
