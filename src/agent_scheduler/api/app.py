"""FastAPI control plane, Worker WSS, and anonymous observation surfaces."""

from __future__ import annotations

import asyncio
import secrets
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
from agent_scheduler.worker.driver import DockerWorkerDriver, FakeWorkerDriver
from agent_scheduler.worker.gpu import HySmiSampler
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
    sampler: HySmiSampler | None
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
        )
        if settings.harness_mode == "claude"
        else FakeHarnessAdapter()
    )
    hub: WorkerHub | None = None
    sampler: HySmiSampler | None = None
    driver: WorkerDriver
    if settings.worker_mode == "fake":
        driver = FakeWorkerDriver()
    elif settings.worker_mode == "local":
        driver = DockerWorkerDriver(
            DockerCLI(), events, identity.signing_public_key, identity.key_id
        )
        sampler = HySmiSampler(settings.vram_threshold)
    else:
        hub = WorkerHub(events, lambda _worker: None)
        driver = RemoteWorkerDriver(hub, str(settings.state_root), settings.worker_id)
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
    )
    context = AppContext(settings, events, identity, proposals, scheduler, hub, sampler)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        sampler_task: asyncio.Task[None] | None = None
        scheduler_task: asyncio.Task[None] | None = None
        prune_framework_logs(settings.state_root)
        if settings.worker_mode == "fake":
            now = utc_now()
            scheduler.register_worker(
                WorkerSnapshot(
                    worker_id=settings.worker_id,
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
        elif sampler is not None:
            sampler_task = asyncio.create_task(
                _sample_local_worker(scheduler, sampler, settings.worker_id)
            )
        if settings.auto_schedule:
            scheduler_task = asyncio.create_task(_scheduler_loop(scheduler))
        try:
            yield
        finally:
            for task in (sampler_task, scheduler_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (sampler_task, scheduler_task) if task is not None),
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
            "workers": len(scheduler.workers()),
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
        container: dict[str, object]
        try:
            inspection = DockerCLI().inspect("fh-sglang-deepseek-v4-flash")
            container = {
                "name": "fh-sglang-deepseek-v4-flash",
                "exists": inspection.exists,
                "running": inspection.running,
                "image_id": inspection.image_id,
            }
        except DockerError as exc:
            container = {
                "name": "fh-sglang-deepseek-v4-flash",
                "exists": False,
                "running": False,
                "error": type(exc).__name__,
            }
        framework_logs = [
            str(path.relative_to(settings.state_root))
            for path in sorted((settings.state_root / "framework-logs").rglob("*.json"))
        ]
        return {
            "request_id": request_id,
            "master": {"draining": context.draining, "profile": _profile(settings)},
            "workers": [item.model_dump(mode="json") for item in scheduler.workers()],
            "gpus": [
                gpu.model_dump(mode="json") for worker in scheduler.workers() for gpu in worker.gpus
            ],
            "proposals": [item.model_dump(mode="json") for item in proposals.list_proposals()],
            "reviews": [item.model_dump(mode="json") for item in proposals.list_reviews()],
            "tasks": [item.model_dump(mode="json") for item in scheduler.statuses()],
            "units": [unit.model_dump(mode="json") for task in tasks for unit in task.units],
            "queue": scheduler.queued_task_ids(),
            "plans": [item.model_dump(mode="json") for item in scheduler.plans()],
            "leases": [item.model_dump(mode="json") for item in scheduler.leases()],
            "containers": [container],
            "framework_logs": framework_logs,
            "audit_streams": [
                str(path.relative_to(events.events_root))
                for path in sorted(events.events_root.rglob("*.jsonl"))
            ],
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
        expected = f"Bearer {identity.worker_api_key}"
        if worker_id != settings.worker_id or not secrets.compare_digest(authorization, expected):
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


async def _scheduler_loop(scheduler: Scheduler) -> None:
    while True:
        await asyncio.to_thread(scheduler.tick, "scheduler-loop")
        await asyncio.sleep(1)


async def _sample_local_worker(scheduler: Scheduler, sampler: HySmiSampler, worker_id: str) -> None:
    while True:
        snapshots = await asyncio.to_thread(sampler.sample)
        scheduler.register_worker(
            WorkerSnapshot(
                worker_id=worker_id,
                online=True,
                last_heartbeat_at=utc_now(),
                gpus=snapshots,
            )
        )
        await asyncio.sleep(10)


def _request_id(request: Request) -> str:
    return request.headers.get("X-Request-ID", str(uuid.uuid4()))


def _proposal_http_error(exc: ProposalError, request_id: str) -> HTTPException:
    status_code = 404 if exc.code == "NOT_FOUND" else 409
    return _error(status_code, exc.code, str(exc), request_id)


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


_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Agent GPU Scheduler</title><style>
:root{font-family:ui-monospace,monospace;color:#102019;background:#eef4ef}body{max-width:1200px;margin:auto;padding:24px}
h1{font-family:system-ui;margin-bottom:4px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:white;border:1px solid #b7c8ba;border-radius:8px;padding:14px;box-shadow:0 2px 8px #10201912}
.good{color:#156b3a}.bad{color:#a13030}pre{white-space:pre-wrap;overflow:auto;font-size:12px}small{color:#52665a}
</style></head><body><h1>Agent GPU Scheduler</h1><small>匿名只读 · 每 5 秒刷新</small>
<div id="cards" class="grid"></div><pre id="raw"></pre><script>
async function refresh(){const r=await fetch('/api/v1/observe/summary');const d=await r.json();
const cards=document.querySelector('#cards');cards.innerHTML='';
for(const w of d.workers){const e=document.createElement('section');e.className='card';
e.innerHTML=`<b>${w.worker_id}</b><p class="${w.online?'good':'bad'}">${w.online?'ONLINE':'OFFLINE'}</p>`+
w.gpus.map(g=>`GPU ${g.gpu_id}: ${g.vram_percent}% ${g.state}`).join('<br>');cards.appendChild(e)}
const q=document.createElement('section');q.className='card';q.innerHTML=`<b>Tasks</b><p>${d.tasks.length}</p>`+
d.tasks.map(t=>`${t.task_id.slice(0,18)}… ${t.state}`).join('<br>');cards.appendChild(q);
document.querySelector('#raw').textContent=JSON.stringify(d.proposals,null,2)}refresh();setInterval(refresh,5000);
</script></body></html>"""
