from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

from agent_scheduler.config import Settings, WorkerSettings
from agent_scheduler.runtime import init_runtime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "provision_node.py"


def run(*arguments: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *(str(value) for value in arguments)],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_master_provisioning_creates_strict_config_and_independent_keys(tmp_path: Path):
    state = tmp_path / "state"
    config = tmp_path / "master-config"
    init_runtime(state)

    completed = run(
        "master",
        "--state-root",
        state,
        "--config-dir",
        config,
        "--worker-id",
        "worker-a",
        "--worker-id",
        "worker-b",
    )

    assert completed.returncode == 0, completed.stderr
    settings = Settings.from_file(config / "master.json")
    assert settings.allowed_worker_ids == ("worker-a", "worker-b")
    assert settings.max_workers == 2
    first = config / "worker-a.key"
    second = config / "worker-b.key"
    assert first.read_bytes() != second.read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE((config / "master.json").stat().st_mode) == 0o600

    repeated = run(
        "master",
        "--state-root",
        state,
        "--config-dir",
        config,
        "--worker-id",
        "worker-a",
    )
    assert repeated.returncode != 0
    assert "refusing to overwrite" in repeated.stderr


def test_worker_provisioning_installs_only_its_key_ca_and_config(tmp_path: Path):
    state = tmp_path / "state"
    master_config = tmp_path / "master-config"
    worker_config = tmp_path / "worker-config"
    init_runtime(state)
    master = run(
        "master",
        "--state-root",
        state,
        "--config-dir",
        master_config,
        "--worker-id",
        "worker-a",
    )
    assert master.returncode == 0, master.stderr

    completed = run(
        "worker",
        "--state-root",
        state,
        "--config-dir",
        worker_config,
        "--worker-id",
        "worker-a",
        "--master-uri",
        "wss://localhost:8443/api/v1/worker/ws",
        "--api-key-source",
        master_config / "worker-a.key",
        "--ca-source",
        state / "tls" / "certificate.pem",
    )

    assert completed.returncode == 0, completed.stderr
    settings = WorkerSettings.from_file(worker_config / "worker.json")
    assert settings.worker_id == "worker-a"
    assert settings.api_key == (master_config / "worker-a.key").read_text().strip()
    assert stat.S_IMODE((worker_config / "worker.key").stat().st_mode) == 0o600
    assert not (worker_config / "tls-private-key.pem").exists()
    assert not (worker_config / "ed25519-private.pem").exists()


def test_worker_provisioning_rejects_master_host_absent_from_certificate(tmp_path: Path):
    state = tmp_path / "state"
    init_runtime(state)
    key = tmp_path / "worker.key"
    key.write_text("k" * 32, encoding="utf-8")

    completed = run(
        "worker",
        "--state-root",
        state,
        "--config-dir",
        tmp_path / "worker-config",
        "--worker-id",
        "worker-a",
        "--master-uri",
        "wss://master.example:8443/api/v1/worker/ws",
        "--api-key-source",
        key,
        "--ca-source",
        state / "tls" / "certificate.pem",
    )

    assert completed.returncode != 0
    assert "absent from certificate SAN" in completed.stderr
