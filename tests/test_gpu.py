import subprocess

import pytest

from agent_scheduler.domain.models import GpuState
from agent_scheduler.worker.gpu import GpuSamplingError, HySmiSampler


def output(vram: int) -> str:
    header = "DCU Temp AvgPwr Perf PwrCap VRAM% DCU% Mode\n"
    rows = "\n".join(f"{index} 50.0C 100.0W auto 400.0W {vram}% 0.0% Normal" for index in range(8))
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


def test_missing_gpu_row_is_rejected(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["hy-smi"], 0, output(1).rsplit("\n", 2)[0], ""
        ),
    )
    with pytest.raises(GpuSamplingError, match="exactly"):
        HySmiSampler(2).sample()
