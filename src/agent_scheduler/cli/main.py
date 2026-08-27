"""Auditable process and Admin CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
import uvicorn
from agent_scheduler_client.cli import run_mcp

from agent_scheduler.adapters.harness import ClaudeCodeAdapter, FakeHarnessAdapter
from agent_scheduler.adapters.onboarding import HARNESSES
from agent_scheduler.config import Settings
from agent_scheduler.domain.models import new_id
from agent_scheduler.qualification import (
    QualificationResult,
    run_submitter_agent,
    verify_qualification,
)
from agent_scheduler.runtime import init_runtime, load_runtime, load_tls_certificate
from agent_scheduler.storage.events import EventStore
from agent_scheduler.worker.client import WorkerClient
from agent_scheduler.worker.docker import DockerCLI
from agent_scheduler.worker.driver import DockerWorkerDriver
from agent_scheduler.worker.gpu import HySmiSampler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-scheduler")
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init-runtime", help="create runtime identities once")
    init.add_argument("--state-root", type=Path, required=True)

    serve = subparsers.add_parser("serve", help="start loopback HTTPS/WSS Master")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8443)

    worker = subparsers.add_parser("worker", help="start the outbound WSS Worker")
    worker.add_argument("--uri", default="wss://127.0.0.1:8443/api/v1/worker/ws")

    mcp = subparsers.add_parser("mcp", help="run Submitter MCP Adapter on stdio")
    mcp.add_argument("--base-url", default="https://127.0.0.1:8443")
    mcp.add_argument("--username", default=os.environ.get("AGENT_SCHEDULER_USERNAME"))

    qualify = subparsers.add_parser("qualify", help="run real four-task qualification")
    qualify.add_argument("--base-url", default="https://127.0.0.1:8443")
    qualify.add_argument("--timeout", type=int, default=45 * 60)
    qualify.add_argument("--harness", choices=HARNESSES, default="claude")

    inspect = subparsers.add_parser("inspect", help="inspect Ground Truth objects/resources")
    inspect.add_argument("--state-root", type=Path, required=True)
    inspect.add_argument(
        "--kind", choices=("events", "immutable", "snapshot", "leases"), default="events"
    )
    inspect.add_argument("--object-type")
    inspect.add_argument("--object-id")

    for name in ("tick", "drain"):
        admin = subparsers.add_parser(name, help=f"invoke Admin {name}")
        _admin_arguments(admin)
    compile_retry = subparsers.add_parser(
        "compile-retry", help="retry a frozen Compilation Context"
    )
    _admin_arguments(compile_retry)
    compile_retry.add_argument("--proposal-id", required=True)
    reconcile = subparsers.add_parser("reconcile", help="atomically reconcile an execution")
    _admin_arguments(reconcile)
    reconcile.add_argument("--execution-id", required=True)
    reload_users = subparsers.add_parser("reload-users", help="reload username allowlist")
    _admin_arguments(reload_users)
    reload_users.add_argument("--users", nargs="+", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init-runtime":
        print(json.dumps(init_runtime(args.state_root), indent=2))
        return 0
    if args.command == "serve":
        settings = Settings.from_env()
        identity = load_runtime(settings.state_root)
        uvicorn.run(
            "agent_scheduler.api.entrypoint:app",
            host=args.host,
            port=args.port,
            ssl_certfile=str(identity.tls_certificate),
            ssl_keyfile=str(identity.tls_private_key),
            log_level="info",
        )
        return 0
    if args.command == "worker":
        settings = Settings.from_env()
        identity = load_runtime(settings.state_root)
        event_store = EventStore(settings.state_root)
        driver = DockerWorkerDriver(
            DockerCLI(),
            event_store,
            identity.signing_public_key,
            identity.key_id,
            worker_id=settings.worker_id,
        )
        project_root = Path(__file__).resolve().parents[3]
        controller = (
            ClaudeCodeAdapter(
                event_store,
                prompts_dir=project_root / "prompts",
                mcp_config=project_root / "config" / "empty-mcp.json",
                timeout_seconds=300,
            )
            if settings.harness_mode == "claude"
            else FakeHarnessAdapter()
        )
        client = WorkerClient(
            uri=args.uri,
            worker_id=settings.worker_id,
            api_key=identity.worker_api_key,
            driver=driver,
            sampler=HySmiSampler(settings.vram_threshold),
            ca_file=str(identity.tls_certificate),
            controller=controller,
        )
        asyncio.run(client.run())
        return 0
    if args.command == "mcp":
        if not args.username:
            raise SystemExit("mcp requires --username or AGENT_SCHEDULER_USERNAME")
        settings = Settings.from_env()
        return run_mcp(
            base_url=args.base_url,
            username=args.username,
            ca_file=load_tls_certificate(settings.state_root),
        )
    if args.command == "qualify":
        try:
            settings = Settings.from_env()
            identity = load_runtime(settings.state_root)
            project_root = Path(__file__).resolve().parents[3]
            result = run_submitter_agent(
                project_root=project_root,
                state_root=settings.state_root,
                base_url=args.base_url,
                tls_certificate=identity.tls_certificate,
                timeout_seconds=args.timeout,
                harness=args.harness,
            )
            verified = verify_qualification(
                result,
                state_root=settings.state_root,
                identity=identity,
                harness=args.harness,
            )
        except (OSError, TypeError, ValueError) as exc:
            verified = QualificationResult(
                run_id=new_id("qual"),
                status="BLOCKED_QUALIFICATION",
                items=(),
                reason=f"qualification precondition failed: {type(exc).__name__}: {exc}",
            )
        print(verified.model_dump_json(indent=2))
        return 0 if verified.status == "COMPLETED" else 3
    if args.command == "inspect":
        store = EventStore(args.state_root)
        if args.kind == "leases":
            values = list(store.iter_snapshots("leases"))
        else:
            if not args.object_type or not args.object_id:
                raise SystemExit("inspect requires --object-type and --object-id")
            if args.kind == "events":
                values = [
                    event.model_dump(mode="json")
                    for event in store.list_events(args.object_type, args.object_id)
                ]
            elif args.kind == "immutable":
                values = [store.read_immutable(args.object_type, args.object_id)]
            else:
                snapshot = store.read_snapshot(args.object_type, args.object_id)
                values = [snapshot] if snapshot is not None else []
        print(json.dumps(values, indent=2))
        return 0
    if args.command in {"tick", "drain", "compile-retry", "reconcile", "reload-users"}:
        return _run_admin(args)
    return 2


def _admin_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", default="https://127.0.0.1:8443")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)


def _run_admin(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    identity = load_runtime(settings.state_root)
    body: dict[str, object] = {"actor": args.actor, "reason": args.reason}
    if args.command == "tick":
        path = "/api/v1/admin/tick"
    elif args.command == "drain":
        path = "/api/v1/admin/drain"
    elif args.command == "compile-retry":
        path = f"/api/v1/admin/proposals/{args.proposal_id}/retry-compilation"
    elif args.command == "reconcile":
        path = f"/api/v1/admin/executions/{args.execution_id}/reconcile"
    else:
        path = "/api/v1/admin/reload-users"
        body["users"] = args.users
    response = httpx.post(
        f"{args.base_url}{path}",
        json=body,
        verify=str(identity.tls_certificate),
        timeout=30,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
