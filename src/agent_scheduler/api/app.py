"""FastAPI control plane, Worker WSS, and anonymous observation surfaces."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agent_scheduler.adapters.harness import ClaudeCodeAdapter, FakeHarnessAdapter
from agent_scheduler.config import Settings
from agent_scheduler.domain.models import GpuSnapshot, GpuState, WorkerSnapshot, utc_now
from agent_scheduler.proposal.service import ProposalError, ProposalService
from agent_scheduler.runtime import RuntimeIdentity, load_runtime
from agent_scheduler.scheduler.core import Scheduler, SchedulingError, WorkerDriver
from agent_scheduler.storage import EventStore, StoreCorruptionError, prune_framework_logs
from agent_scheduler.worker.docker import DockerCLI, DockerError
from agent_scheduler.worker.driver import FakeWorkerDriver
from agent_scheduler.worker.protocol import RemoteWorkerDriver, WorkerHub


class MarkdownRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    markdown: str = Field(min_length=1, max_length=256 * 1024)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    revision_id: str


class AdminReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1024)


class ReloadUsersRequest(AdminReasonRequest):
    users: tuple[str, ...] = Field(min_length=1)


@dataclass
class AppContext:
    settings: Settings
    events: EventStore
    identity: RuntimeIdentity
    proposals: ProposalService
    scheduler: Scheduler
    hub: WorkerHub | None
    draining: bool = False


def create_app(settings: Settings, identity: RuntimeIdentity | None = None) -> FastAPI:
    identity = identity or load_runtime(settings.state_root)
    events = EventStore(settings.state_root)
    project_root = Path(__file__).resolve().parents[3]
    harness = (
        ClaudeCodeAdapter(
            events,
            prompts_dir=project_root / "prompts",
            mcp_config=project_root / "config" / "empty-mcp.json",
            allowed_worker_ids=settings.allowed_worker_ids,
        )
        if settings.harness_mode == "claude"
        else FakeHarnessAdapter(worker_id=settings.allowed_worker_ids[0])
    )
    hub: WorkerHub | None = None
    driver: WorkerDriver
    if settings.worker_mode == "fake":
        driver = FakeWorkerDriver()
    else:
        hub = WorkerHub(events, lambda _worker: None)
        driver = RemoteWorkerDriver(hub, str(settings.state_root))
    scheduler = Scheduler(
        events,
        identity.signing_private_key,
        identity.signing_public_key,
        driver,
        key_id=identity.key_id,
        vram_threshold=settings.vram_threshold,
        qualification_profile=settings.qualification_profile,
    )
    if hub is not None:
        hub.on_heartbeat = scheduler.register_worker
    proposals = ProposalService(
        events,
        harness,
        identity.signing_private_key,
        key_id=identity.key_id,
        allowed_users=set(settings.allowed_users),
        max_workers=settings.max_workers,
        allowed_worker_ids=settings.allowed_worker_ids,
    )
    context = AppContext(settings, events, identity, proposals, scheduler, hub)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        scheduler_task: asyncio.Task[None] | None = None
        prune_framework_logs(settings.state_root)
        if settings.worker_mode == "fake":
            now = utc_now()
            scheduler.register_worker(
                WorkerSnapshot(
                    worker_id=settings.allowed_worker_ids[0],
                    online=True,
                    last_heartbeat_at=now,
                    gpus=tuple(
                        GpuSnapshot(
                            gpu_id=index,
                            vram_percent=0.0,
                            sampled_at=now,
                            state=GpuState.AVAILABLE,
                            raw_line="fake",
                        )
                        for index in range(8)
                    ),
                )
            )
        if settings.auto_schedule:
            scheduler_task = asyncio.create_task(_scheduler_loop(scheduler))
        try:
            yield
        finally:
            for task in (scheduler_task,):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (scheduler_task,) if task is not None),
                return_exceptions=True,
            )

    app = FastAPI(title="Agent GPU Task Scheduler", version="0.2.0", lifespan=lifespan)
    app.state.context = context

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        request_id = _request_id(request)
        return JSONResponse(
            status_code=422,
            content=_error_body(
                "VALIDATION_ERROR",
                "request failed strict schema validation",
                request_id,
                details=exc.errors(),
            ),
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = _request_id(request)
        if isinstance(exc.detail, dict) and "error_code" in exc.detail:
            body = dict(exc.detail)
            body.setdefault("request_id", request_id)
        else:
            body = _error_body(
                "METHOD_NOT_ALLOWED" if exc.status_code == 405 else "HTTP_ERROR",
                str(exc.detail),
                request_id,
            )
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.get("/health")
    def health() -> dict[str, object]:
        try:
            events.validate_all_events()
            integrity = "valid"
        except StoreCorruptionError as exc:
            raise _error(503, "GROUND_TRUTH_CORRUPT", str(exc), str(uuid.uuid4())) from exc
        return {
            "status": "ready" if not context.draining else "draining",
            "workers": sum(worker.online for worker in scheduler.workers()),
            "configured_workers": len(settings.allowed_worker_ids),
            "integrity": integrity,
            "qualification": settings.qualification_profile,
            "harness_mode": settings.harness_mode,
            "worker_mode": settings.worker_mode,
        }

    @app.post("/api/v1/proposals", status_code=201)
    def create_proposal(
        body: MarkdownRequest,
        request: Request,
        x_username: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        _reject_if_draining(context)
        request_id = _request_id(request)
        try:
            proposal = proposals.create(
                x_username or "", body.markdown, idempotency_key or "", request_id
            )
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        return {"request_id": request_id, "proposal": proposal.model_dump(mode="json")}

    @app.get("/api/v1/proposals/{proposal_id}")
    def get_proposal(proposal_id: str, request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        try:
            proposal = proposals.get(proposal_id)
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        return {"request_id": request_id, "proposal": proposal.model_dump(mode="json")}

    @app.get("/api/v1/proposals/{proposal_id}/reviews")
    def get_reviews(proposal_id: str, request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        try:
            reviews = proposals.get_reviews(proposal_id)
            facts = proposals.get_current_facts(proposal_id)
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        return {
            "request_id": request_id,
            "reviews": [review.model_dump(mode="json") for review in reviews],
            "current_facts": facts.model_dump(mode="json") if facts else None,
        }

    @app.get("/api/v1/proposals/{proposal_id}/events")
    def proposal_events(
        proposal_id: str, request: Request, after_sequence: int = Query(default=0, ge=0)
    ) -> dict[str, object]:
        request_id = _request_id(request)
        values = events.list_events("proposals", proposal_id, after_sequence)
        return {
            "request_id": request_id,
            "events": [value.model_dump(mode="json") for value in values],
        }

    @app.post("/api/v1/proposals/{proposal_id}/replies")
    def reply(
        proposal_id: str,
        body: MarkdownRequest,
        request: Request,
        x_username: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        _reject_if_draining(context)
        request_id = _request_id(request)
        try:
            proposal = proposals.reply(
                proposal_id,
                x_username or "",
                body.markdown,
                idempotency_key or "",
                request_id,
            )
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        return {"request_id": request_id, "proposal": proposal.model_dump(mode="json")}

    @app.post("/api/v1/proposals/{proposal_id}/confirm")
    def confirm(
        proposal_id: str,
        body: ConfirmRequest,
        request: Request,
        x_username: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        _reject_if_draining(context)
        request_id = _request_id(request)
        try:
            task = proposals.confirm(
                proposal_id,
                x_username or "",
                body.revision_id,
                idempotency_key or "",
                request_id,
            )
            task_status = scheduler.enqueue(task, request_id)
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        except SchedulingError as exc:
            raise _error(409, "SCHEDULING_REJECTED", str(exc), request_id) from exc
        return {
            "request_id": request_id,
            "task": task.model_dump(mode="json"),
            "status": task_status.model_dump(mode="json"),
        }

    @app.post("/api/v1/proposals/{proposal_id}/resume")
    def resume(
        proposal_id: str,
        request: Request,
        x_username: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> dict[str, object]:
        _reject_if_draining(context)
        request_id = _request_id(request)
        try:
            proposal = proposals.resume(
                proposal_id, x_username or "", idempotency_key or "", request_id
            )
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        return {"request_id": request_id, "proposal": proposal.model_dump(mode="json")}

    @app.post("/api/v1/proposals/{proposal_id}/cancel")
    def cancel(
        proposal_id: str,
        request: Request,
        x_username: str | None = Header(default=None),
    ) -> dict[str, object]:
        request_id = _request_id(request)
        try:
            proposal = proposals.cancel(proposal_id, x_username or "", request_id)
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        return {"request_id": request_id, "proposal": proposal.model_dump(mode="json")}

    @app.post("/api/v1/tasks/{task_id}/cancel")
    def cancel_task(
        task_id: str,
        request: Request,
        x_username: str | None = Header(default=None),
    ) -> dict[str, object]:
        request_id = _request_id(request)
        try:
            task = scheduler.get_task(task_id)
            if not task.units or task.units[0].submitter_username != (x_username or ""):
                raise SchedulingError("username does not own Task declaration")
            task_status = scheduler.cancel_task(
                task_id, actor=x_username or "", request_id=request_id
            )
        except SchedulingError as exc:
            raise _error(409, "CANCEL_REJECTED", str(exc), request_id) from exc
        return {"request_id": request_id, "status": task_status.model_dump(mode="json")}

    @app.get("/api/v1/tasks/{task_id}")
    def get_task(task_id: str, request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        try:
            task = scheduler.get_task(task_id)
            task_status = scheduler.get_status(task_id)
        except SchedulingError as exc:
            raise _error(404, "NOT_FOUND", str(exc), request_id) from exc
        return {
            "request_id": request_id,
            "task": task.model_dump(mode="json"),
            "status": task_status.model_dump(mode="json"),
        }

    @app.get("/api/v1/logs/{task_id}/{unit_id}/{execution_id}/{name}")
    def get_log(
        task_id: str,
        unit_id: str,
        execution_id: str,
        name: str,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=64 * 1024, ge=1, le=1024 * 1024),
    ) -> Response:
        root = (settings.state_root / "framework-logs").resolve()
        path = (root / task_id / unit_id / execution_id / name).resolve()
        if root not in path.parents or not path.is_file():
            raise _error(404, "LOG_NOT_FOUND", "log not found", str(uuid.uuid4()))
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(limit)
        return Response(
            data,
            media_type="application/octet-stream",
            headers={"X-Next-Offset": str(offset + len(data))},
        )

    @app.get("/api/v1/observe/logs/{task_id}/{unit_id}/{execution_id}/{name}")
    def observe_log(
        task_id: str,
        unit_id: str,
        execution_id: str,
        name: str,
        request: Request,
        offset: int | None = Query(default=None, ge=0),
        tail_lines: int = Query(default=1000, ge=1, le=10000),
    ) -> dict[str, object]:
        request_id = _request_id(request)
        root = (settings.state_root / "framework-logs").resolve()
        path = (root / task_id / unit_id / execution_id / name).resolve()
        if root not in path.parents or not path.is_file():
            raise _error(404, "LOG_NOT_FOUND", "log not found", request_id)
        data = path.read_bytes()
        start = (
            offset
            if offset is not None
            else max(
                0,
                len(data) - len(b"\n".join(data.splitlines()[-tail_lines:])),
            )
        )
        chunk = data[start:]
        return {
            "request_id": request_id,
            "data": chunk.decode("utf-8", errors="replace"),
            "offset": start,
            "next_offset": len(data),
        }

    @app.post("/api/v1/admin/tick")
    def tick(body: AdminReasonRequest, request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        changed = scheduler.tick(request_id)
        events.append(
            "admin",
            _admin_object_id(),
            "SCHEDULER_TICK",
            body.actor,
            request_id,
            {"reason": body.reason, "changed": [item.task_id for item in changed]},
        )
        return {
            "request_id": request_id,
            "statuses": [item.model_dump(mode="json") for item in changed],
        }

    @app.post("/api/v1/admin/drain")
    def drain(body: AdminReasonRequest, request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        before = context.draining
        context.draining = True
        events.append(
            "admin",
            _admin_object_id(),
            "DRAINED",
            body.actor,
            request_id,
            {"reason": body.reason, "before": before, "after": True},
        )
        return {"request_id": request_id, "draining": True}

    @app.post("/api/v1/admin/proposals/{proposal_id}/retry-compilation")
    def retry_compilation(
        proposal_id: str, body: AdminReasonRequest, request: Request
    ) -> dict[str, object]:
        request_id = _request_id(request)
        try:
            before = proposals.get(proposal_id).state.value
            task = proposals.retry_compilation(proposal_id, body.actor, request_id)
            task_status = scheduler.enqueue(task, request_id)
            after = proposals.get(proposal_id).state.value
        except ProposalError as exc:
            raise _proposal_http_error(exc, request_id) from exc
        events.append(
            "admin",
            _admin_object_id(),
            "COMPILATION_RETRY",
            body.actor,
            request_id,
            {
                "reason": body.reason,
                "proposal_id": proposal_id,
                "before": before,
                "after": after,
                "task_id": task.task_id,
            },
        )
        return {
            "request_id": request_id,
            "task": task.model_dump(mode="json"),
            "status": task_status.model_dump(mode="json"),
        }

    @app.post("/api/v1/admin/executions/{execution_id}/reconcile")
    def reconcile(
        execution_id: str, body: AdminReasonRequest, request: Request
    ) -> dict[str, object]:
        request_id = _request_id(request)
        before = [
            lease.model_dump(mode="json")
            for lease in scheduler.leases()
            if lease.execution_id == execution_id
        ]
        try:
            released = scheduler.reconcile_execution(
                execution_id, actor=body.actor, reason=body.reason, request_id=request_id
            )
        except SchedulingError as exc:
            raise _error(409, "RECONCILE_REJECTED", str(exc), request_id) from exc
        after = [
            lease.model_dump(mode="json")
            for lease in scheduler.leases()
            if lease.execution_id == execution_id
        ]
        events.append(
            "admin",
            _admin_object_id(),
            "RECONCILE_COMMAND",
            body.actor,
            request_id,
            {
                "reason": body.reason,
                "execution_id": execution_id,
                "before": before,
                "after": after,
                "released": released,
            },
        )
        return {"request_id": request_id, "released": released}

    @app.post("/api/v1/admin/reload-users")
    def reload_users(body: ReloadUsersRequest, request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        before = sorted(proposals.allowed_users)
        proposals.allowed_users = set(body.users)
        events.append(
            "admin",
            _admin_object_id(),
            "USERS_RELOADED",
            body.actor,
            request_id,
            {
                "reason": body.reason,
                "before": before,
                "after": sorted(body.users),
            },
        )
        return {"request_id": request_id, "users": sorted(body.users)}

    @app.get("/api/v1/observe/summary")
    def observe_summary(request: Request) -> dict[str, object]:
        request_id = _request_id(request)
        tasks = proposals.list_tasks()
        container = _container_summary("fh-sglang-deepseek-v4-flash")
        return {
            "request_id": request_id,
            "master": {
                "draining": context.draining,
                "profile": _profile(settings),
                "integrity": _cached_integrity(events),
                "harness_mode": settings.harness_mode,
                "worker_mode": settings.worker_mode,
            },
            "workers": [item.model_dump(mode="json") for item in scheduler.workers()],
            "gpus": [
                gpu.model_dump(mode="json") for worker in scheduler.workers() for gpu in worker.gpus
            ],
            "proposals": [item.model_dump(mode="json") for item in proposals.list_proposals()],
            "reviews": [item.model_dump(mode="json") for item in proposals.list_reviews()],
            "tasks": [item.model_dump(mode="json") for item in scheduler.statuses()],
            # TaskUnit carries no back-reference, so the observation surface adds the owning
            # Task and Proposal; without it the dashboard cannot join a unit to its Proposal.
            "units": [
                {
                    **unit.model_dump(mode="json"),
                    "task_id": task.task_id,
                    "execution_id": task.execution_id,
                    "proposal_id": task.proposal_id,
                }
                for task in tasks
                for unit in task.units
            ],
            "queue": scheduler.queued_task_ids(),
            "plans": [item.model_dump(mode="json") for item in scheduler.plans()],
            "leases": [item.model_dump(mode="json") for item in scheduler.leases()],
            "containers": [container],
            "framework_logs": _log_index(settings.state_root / "framework-logs", "*.json"),
            "audit_streams": _log_index(events.events_root, "*.jsonl"),
        }

    @app.get("/api/v1/observe/events/{object_type}/{object_id}")
    def observe_events(
        object_type: str,
        object_id: str,
        request: Request,
        after_sequence: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        request_id = _request_id(request)
        values = events.list_events(object_type, object_id, after_sequence)
        return {
            "request_id": request_id,
            "events": [value.model_dump(mode="json") for value in values],
        }

    @app.websocket("/api/v1/worker/ws")
    async def worker_ws(websocket: WebSocket) -> None:
        if hub is None:
            await websocket.close(code=4403, reason="remote Worker mode is disabled")
            return
        worker_id = websocket.headers.get("X-Worker-ID", "")
        authorization = websocket.headers.get("Authorization", "")
        configured_keys = dict(settings.worker_api_keys)
        api_key = configured_keys.get(worker_id)
        if api_key is None and not configured_keys and worker_id in settings.allowed_worker_ids:
            api_key = identity.worker_api_key
        expected = f"Bearer {api_key}" if api_key is not None else ""
        if not expected or not secrets.compare_digest(authorization, expected):
            await websocket.close(code=4401, reason="invalid Worker identity")
            return
        await hub.serve(worker_id, websocket)

    @app.get("/")
    def index() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.api_route(
        "/api/v1/observe/{path:path}",
        methods=["POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    def observe_write_rejected(path: str) -> Response:
        raise HTTPException(status_code=405, detail="observe namespace is GET-only")

    return app


_LOOP_LOG = logging.getLogger("agent_scheduler.loops")


def _tick_safely(scheduler: Scheduler) -> None:
    try:
        scheduler.tick("scheduler-loop")
    except Exception:  # noqa: BLE001 — a background loop must survive any transient failure; one uncaught raise kills scheduling for good.
        _LOOP_LOG.warning("scheduler tick failed", exc_info=True)
        return


async def _scheduler_loop(scheduler: Scheduler) -> None:
    while True:
        await asyncio.to_thread(_tick_safely, scheduler)
        await asyncio.sleep(1)


def _request_id(request: Request) -> str:
    return request.headers.get("X-Request-ID", str(uuid.uuid4()))


# The dashboard polls every 2s, so every uncached observe request would fork a `docker inspect`
# and re-walk two unbounded directory trees. One refresh window of staleness is invisible on
# screen and bounds the cost to one pass per window no matter how many tabs are open.
_OBSERVE_CACHE_TTL_SECONDS = 2.0
# Integrity validation parses every event file, so it must not run at the poll rate. Ground
# Truth corruption is a standing condition, not a transient, and half a minute of staleness
# costs an operator nothing.
_INTEGRITY_CACHE_TTL_SECONDS = 30.0
_LOG_INDEX_RECENT = 20
_CONTAINER_CACHE: dict[str, tuple[float, dict[str, object]]] = {}
_LOG_INDEX_CACHE: dict[tuple[str, str], tuple[float, dict[str, object]]] = {}
_INTEGRITY_CACHE: dict[str, tuple[float, str]] = {}


def _cached_integrity(events: EventStore) -> str:
    key = str(events.events_root)
    cached = _INTEGRITY_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _INTEGRITY_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        events.validate_all_events()
        integrity = "valid"
    except StoreCorruptionError as exc:
        integrity = type(exc).__name__
    _INTEGRITY_CACHE[key] = (now, integrity)
    return integrity


def _container_summary(name: str) -> dict[str, object]:
    cached = _CONTAINER_CACHE.get(name)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _OBSERVE_CACHE_TTL_SECONDS:
        return cached[1]
    summary: dict[str, object]
    try:
        inspection = DockerCLI().inspect(name)
        summary = {
            "name": name,
            "exists": inspection.exists,
            "running": inspection.running,
            "image_id": inspection.image_id,
        }
    except DockerError as exc:
        summary = {
            "name": name,
            "exists": False,
            "running": False,
            "error": type(exc).__name__,
        }
    _CONTAINER_CACHE[name] = (now, summary)
    return summary


def _log_index(root: Path, pattern: str) -> dict[str, object]:
    """Summarize an append-only tree as a count plus its newest entries.

    Returning every path made the response grow for the life of the deployment. Paths are
    keyed by UUIDv7, which is time-ordered, so a reverse lexicographic sort yields the
    newest entries without a stat() per file.
    """
    key = (str(root), pattern)
    cached = _LOG_INDEX_CACHE.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _OBSERVE_CACHE_TTL_SECONDS:
        return cached[1]
    paths = sorted((str(path.relative_to(root)) for path in root.rglob(pattern)), reverse=True)
    index: dict[str, object] = {"count": len(paths), "recent": paths[:_LOG_INDEX_RECENT]}
    _LOG_INDEX_CACHE[key] = (now, index)
    return index


_PROPOSAL_ERROR_STATUS = {
    "NOT_FOUND": 404,
    "USERNAME_NOT_ALLOWED": 403,
    # Content rejections, not state conflicts: a 409 would tell the caller to retry later,
    # when the only useful response is to correct the submission.
    "INVALID_PROPOSAL": 422,
    "IDEMPOTENCY_REQUIRED": 422,
}


def _proposal_http_error(exc: ProposalError, request_id: str) -> HTTPException:
    return _error(_PROPOSAL_ERROR_STATUS.get(exc.code, 409), exc.code, str(exc), request_id)


def _error_body(
    code: str,
    message: str,
    request_id: str,
    *,
    details: object | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "error_code": code,
        "message": message,
        "object_id": None,
        "current_state": None,
        "request_id": request_id,
        "retryable": False,
    }
    if details is not None:
        body["details"] = details
    return body


def _error(status_code: int, code: str, message: str, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=_error_body(code, message, request_id),
    )


def _reject_if_draining(context: AppContext) -> None:
    if context.draining:
        raise _error(503, "MASTER_DRAINING", "Master is draining", str(uuid.uuid4()))


def _profile(settings: Settings) -> dict[str, object]:
    return {
        "qualification": settings.qualification_profile,
        "vram_threshold": settings.vram_threshold,
        "authorized_gpu_ids": list(range(8)),
    }


def _admin_object_id() -> str:
    return "admin_00000000000070008000000000000000"


_DASHBOARD_HTML = (Path(__file__).with_name("dashboard.html")).read_text(encoding="utf-8")
