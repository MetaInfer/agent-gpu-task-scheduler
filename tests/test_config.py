import json

import pytest

from agent_scheduler.config import Settings, WorkerSettings


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


def _secret(path, value):
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_multiple_workers_and_independent_key_files_are_loaded(tmp_path):
    _secret(tmp_path / "worker-a.key", "a" * 32)
    _secret(tmp_path / "worker-b.key", "b" * 32)
    config = tmp_path / "master.json"
    config.write_text(
        json.dumps(
            {
                "state_root": "state",
                "max_workers": 2,
                "workers": {
                    "worker-a": {"api_key_file": "worker-a.key"},
                    "worker-b": {"api_key_file": "worker-b.key"},
                },
            }
        ),
        encoding="utf-8",
    )

    settings = Settings.from_file(config)

    assert settings.allowed_worker_ids == ("worker-a", "worker-b")
    assert dict(settings.worker_api_keys)["worker-b"] == "b" * 32
    assert settings.state_root == tmp_path / "state"


def test_master_rejects_removed_local_worker_identity(tmp_path):
    _secret(tmp_path / "worker-a.key", "a" * 32)
    config = tmp_path / "master.json"
    config.write_text(
        json.dumps(
            {
                "state_root": "state",
                "worker_mode": "remote",
                "local_worker_id": "worker-a",
                "workers": {"worker-a": {"api_key_file": "worker-a.key"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown fields: local_worker_id"):
        Settings.from_file(config)


def test_master_rejects_permissive_worker_key_file(tmp_path):
    key = tmp_path / "worker-a.key"
    key.write_text("a" * 32, encoding="utf-8")
    key.chmod(0o644)
    config = tmp_path / "master.json"
    config.write_text(
        json.dumps(
            {
                "state_root": "state",
                "workers": {"worker-a": {"api_key_file": "worker-a.key"}},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PermissionError, match="too permissive"):
        Settings.from_file(config)


def test_worker_configuration_uses_files_without_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SCHEDULER_WORKER_ID", "must-not-be-read")
    _secret(tmp_path / "worker.key", "k" * 32)
    (tmp_path / "ca.pem").write_text("test CA", encoding="utf-8")
    config = tmp_path / "worker.json"
    config.write_text(
        json.dumps(
            {
                "state_root": "state",
                "worker_id": "worker-b",
                "master_uri": "wss://master.example:8443/api/v1/worker/ws",
                "api_key_file": "worker.key",
                "ca_file": "ca.pem",
            }
        ),
        encoding="utf-8",
    )

    settings = WorkerSettings.from_file(config)

    assert settings.worker_id == "worker-b"
    assert settings.api_key == "k" * 32
    assert settings.master_uri.startswith("wss://master.example")
