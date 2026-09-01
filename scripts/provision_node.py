#!/usr/bin/env python3
"""Provision and preflight explicit Master/Worker node configuration."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urlparse


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    master = commands.add_parser("master", help="create Master config and per-Worker keys")
    master.add_argument("--state-root", type=Path, required=True)
    master.add_argument("--config-dir", type=Path, default=Path("/etc/agent-scheduler"))
    master.add_argument("--worker-id", action="append", required=True)
    master.add_argument("--harness-mode", choices=("fake", "claude"), default="claude")
    master.add_argument("--profile", choices=("production", "qualification"), default="production")
    master.add_argument("--vram-threshold", type=float)
    master.add_argument("--allowed-user", action="append", default=[])

    worker = commands.add_parser("worker", help="install one Worker's config, key, and CA")
    worker.add_argument("--state-root", type=Path, required=True)
    worker.add_argument("--config-dir", type=Path, default=Path("/etc/agent-scheduler"))
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--master-uri", required=True)
    worker.add_argument("--api-key-source", type=Path, required=True)
    worker.add_argument("--ca-source", type=Path, required=True)
    worker.add_argument("--harness-mode", choices=("fake", "claude"), default="claude")
    worker.add_argument("--profile", choices=("production", "qualification"), default="production")
    worker.add_argument("--vram-threshold", type=float)

    check = commands.add_parser("check", help="validate an existing Master or Worker config")
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--kind", choices=("master", "worker"), required=True)
    return result


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _json_bytes(value: dict[str, object]) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def _threshold(profile: str, supplied: float | None) -> float:
    maximum = 97.0 if profile == "qualification" else 2.0
    value = maximum if supplied is None else supplied
    if value <= 0 or value > maximum:
        raise ValueError(f"VRAM threshold must be in (0, {maximum}] for {profile}")
    return value


def provision_master(args: argparse.Namespace) -> None:
    worker_ids = tuple(args.worker_id)
    if len(set(worker_ids)) != len(worker_ids) or any(not item.strip() for item in worker_ids):
        raise ValueError("Worker IDs must be non-empty and unique")
    required_runtime = (
        args.state_root / "secrets" / "ed25519-private.pem",
        args.state_root / "secrets" / "ed25519-public.pem",
        args.state_root / "secrets" / "ed25519-key-id",
        args.state_root / "secrets" / "tls-private-key.pem",
        args.state_root / "tls" / "certificate.pem",
    )
    missing = [str(path) for path in required_runtime if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "initialize the Master runtime before provisioning: " + ", ".join(missing)
        )
    args.config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.config_dir, 0o700)
    key_paths: dict[str, Path] = {}
    targets = [args.config_dir / "master.json"]
    for worker_id in worker_ids:
        key_paths[worker_id] = args.config_dir / f"{worker_id}.key"
        targets.append(key_paths[worker_id])
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite existing files: " + ", ".join(existing))
    for worker_id in worker_ids:
        _atomic_write(key_paths[worker_id], (secrets.token_urlsafe(32) + "\n").encode(), 0o600)
    config: dict[str, object] = {
        "state_root": str(args.state_root),
        "harness_mode": args.harness_mode,
        "worker_mode": "remote",
        "profile": args.profile,
        "vram_threshold": _threshold(args.profile, args.vram_threshold),
        "allowed_users": args.allowed_user or ["zz_chentian"],
        "max_workers": len(worker_ids),
        "workers": {
            worker_id: {"api_key_file": str(key_paths[worker_id])} for worker_id in worker_ids
        },
        "auto_schedule": True,
    }
    _atomic_write(args.config_dir / "master.json", _json_bytes(config), 0o600)
    print(args.config_dir / "master.json")
    for worker_id, path in key_paths.items():
        print(f"{worker_id}: {path}")


def _certificate_names(path: Path) -> tuple[set[str], set[str]]:
    # The standard library exposes no public API for decoding a certificate file without
    # opening a network connection. This is the same decoder used by ssl.match_hostname.
    decoded = ssl._ssl._test_decode_cert(str(path))
    dns: set[str] = set()
    ips: set[str] = set()
    for kind, value in decoded.get("subjectAltName", ()):  # type: ignore[union-attr]
        if kind == "DNS":
            dns.add(value)
        elif kind == "IP Address":
            ips.add(value)
    return dns, ips


def provision_worker(args: argparse.Namespace) -> None:
    parsed = urlparse(args.master_uri)
    if parsed.scheme != "wss" or not parsed.hostname or parsed.path != "/api/v1/worker/ws":
        raise ValueError("master URI must be wss://<host>[:port]/api/v1/worker/ws")
    if not args.api_key_source.is_file() or not args.ca_source.is_file():
        raise FileNotFoundError("API key source and CA source must both be regular files")
    api_key = args.api_key_source.read_text(encoding="utf-8").strip()
    if len(api_key) < 32:
        raise ValueError("Worker API key must contain at least 32 characters")
    dns, ips = _certificate_names(args.ca_source)
    if parsed.hostname not in dns and parsed.hostname not in ips:
        raise ValueError(
            f"Master URI host {parsed.hostname!r} is absent from certificate SAN "
            f"(DNS={sorted(dns)}, IP={sorted(ips)})"
        )
    public_runtime = (
        args.state_root / "secrets" / "ed25519-public.pem",
        args.state_root / "secrets" / "ed25519-key-id",
    )
    missing = [str(path) for path in public_runtime if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "Worker cannot read shared runtime public material: " + ", ".join(missing)
        )
    args.config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.config_dir, 0o700)
    key_target = args.config_dir / "worker.key"
    ca_target = args.config_dir / "master-ca.pem"
    config_target = args.config_dir / "worker.json"
    existing = [str(path) for path in (key_target, ca_target, config_target) if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite existing files: " + ", ".join(existing))
    _atomic_write(key_target, (api_key + "\n").encode(), 0o600)
    _atomic_write(ca_target, args.ca_source.read_bytes(), 0o644)
    config: dict[str, object] = {
        "state_root": str(args.state_root),
        "worker_id": args.worker_id,
        "master_uri": args.master_uri,
        "api_key_file": str(key_target),
        "ca_file": str(ca_target),
        "harness_mode": args.harness_mode,
        "profile": args.profile,
        "vram_threshold": _threshold(args.profile, args.vram_threshold),
    }
    _atomic_write(config_target, _json_bytes(config), 0o600)
    print(config_target)


def check_config(args: argparse.Namespace) -> None:
    from agent_scheduler.config import Settings, WorkerSettings

    if args.kind == "master":
        master = Settings.from_file(args.config)
        summary: dict[str, object] = {
            "worker_mode": master.worker_mode,
            "worker_ids": list(master.allowed_worker_ids),
            "max_workers": master.max_workers,
        }
    else:
        worker = WorkerSettings.from_file(args.config)
        summary = {"worker_id": worker.worker_id, "master_uri": worker.master_uri}
    print(json.dumps({"kind": args.kind, "config": str(args.config), "valid": True, **summary}))


def main() -> int:
    args = parser().parse_args()
    if args.command == "master":
        provision_master(args)
    elif args.command == "worker":
        provision_worker(args)
    else:
        check_config(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
