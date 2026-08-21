import pytest

from agent_scheduler.config import Settings


def test_qualification_threshold_cannot_exceed_ceiling(monkeypatch):
    monkeypatch.setenv("AGENT_SCHEDULER_PROFILE", "qualification")
    monkeypatch.setenv("AGENT_SCHEDULER_VRAM_THRESHOLD", "100")
    with pytest.raises(ValueError, match="97"):
        Settings.from_env()


def test_production_threshold_cannot_exceed_2(monkeypatch):
    monkeypatch.delenv("AGENT_SCHEDULER_PROFILE", raising=False)
    monkeypatch.setenv("AGENT_SCHEDULER_VRAM_THRESHOLD", "2.1")
    with pytest.raises(ValueError, match="2.0"):
        Settings.from_env()
