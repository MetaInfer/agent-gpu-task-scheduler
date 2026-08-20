"""Auditable process and Admin CLI."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
import uvicorn

from agent_scheduler.adapters.mcp import SubmitterMCPAdapter
from agent_scheduler.config import Settings
from agent_scheduler.runtime import init_runtime, load_runtime
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

    inspect = subparsers.add_parser("inspect", help="inspect persisted events")
    inspect.add_argument("--state-root", type=Path, required=True)
    inspect.add_argument("--object-type", required=True)
    inspect.add_argument("--object-id", required=True)

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
        driver = DockerWorkerDriver(
            DockerCLI(),
            EventStore(settings.state_root),
            identity.signing_public_key,
            identity.key_id,
            worker_id=settings.worker_id,
        )
        client = WorkerClient(
            uri=args.uri,
            worker_id=settings.worker_id,
            api_key=identity.worker_api_key,
            driver=driver,
            sampler=HySmiSampler(settings.vram_threshold),
            ca_file=str(identity.tls_certificate),
        )
        asyncio.run(client.run())
        return 0
    if args.command == "mcp":
        if not args.username:
            raise SystemExit("mcp requires --username or AGENT_SCHEDULER_USERNAME")
        settings = Settings.from_env()
        identity = load_runtime(settings.state_root)
        adapter = SubmitterMCPAdapter(
            args.base_url, args.username, verify=str(identity.tls_certificate)
        )
        try:
            adapter.run_stdio()
        finally:
            adapter.close()
        return 0
    if args.command == "inspect":
        events = EventStore(args.state_root).list_events(args.object_type, args.object_id)
        print(json.dumps([event.model_dump(mode="json") for event in events], indent=2))
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
