import asyncio
import hmac
import http.client
import json
import logging
import secrets
import socket
import ssl
from urllib.parse import urlsplit

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, or_, select, update

from abrl.artifacts import archive
from abrl.auth import configure_auth
from abrl.browser import browser_availability
from abrl.config import API_ORIGIN, CONTROLLED_ORIGIN, WEB_ORIGIN
from abrl.db import (
    Event,
    ManualObservation,
    Run,
    Session,
    Target,
    TargetVersion,
    Trial,
    WorkerHeartbeat,
    Workflow,
    WorkflowVersion,
    record,
)
from abrl.experiment_api import router as experiment_router
from abrl.lab import diagnostics_status
from abrl.lab import router as lab_router
from abrl.list_queries import ordered, text_match
from abrl.network import resolve_public_ipv4
from abrl.observations import router as observation_router
from abrl.platform_api import router as platform_router
from abrl.reports import csv_export, html_export, pdf_export, report, run_view, trial_view
from abrl.runtime import locked_run, relative_time, settle_run
from abrl.schemas import (
    ReportView,
    RunInput,
    RunView,
    TargetInput,
    TargetView,
    TrialView,
    WorkflowInput,
    WorkflowView,
)
from abrl.session_artifacts import router as artifact_router
from abrl.session_template_api import router as template_router
from abrl.telemetry import configure_logging, instrument

configure_logging()
log = logging.getLogger("abrl.api")
PROOF_PATH = "/.well-known/abrl-target.txt"


app = FastAPI(title="ABRL Workflow Lab", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted({WEB_ORIGIN, "http://127.0.0.1:8430", "http://localhost:8430"}),
    allow_methods=["GET", "POST", "PUT"],
    allow_credentials=True,
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-Project-ID", "Idempotency-Key"],
    expose_headers=["X-Total-Count"],
)
app.include_router(lab_router)


def authenticate(request: Request):
    from abrl.access import authorize

    return authorize(request, "operator" if request.method in ("POST", "PUT", "DELETE") else "viewer")


api = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticate)])


def lookup(session, model, id, project_id="default"):
    item = session.get(model, id)
    if item is None or getattr(item, "project_id", "default") != project_id:
        raise HTTPException(404, "Record not found.")
    return item


def target_view(session, item: Target) -> dict:
    config = session.get(TargetVersion, (item.id, item.version)).config
    result = {
        "id": item.id,
        "version": item.version,
        **config,
        "verified": item.verified_version == item.version,
    }
    if not result["verified"]:
        result.update(proof_path=PROOF_PATH, proof_token=item.proof_token)
    return result


def workflow_view(session, item: Workflow) -> dict:
    config = session.get(WorkflowVersion, (item.id, item.version)).config
    return {"id": item.id, "version": item.version, **config}


def locally_owned_origins(data: TargetInput) -> bool:
    return all(
        origin in (API_ORIGIN, CONTROLLED_ORIGIN)
        for origin in [data.origin, *getattr(data, "navigation_origins", [])]
    )


def add_target(session, data: TargetInput, project_id="default") -> Target:
    item = Target(
        project_id=project_id,
        version=1,
        proof_token=secrets.token_urlsafe(32),
        verified_version=1 if locally_owned_origins(data) else 0,
    )
    session.add(item)
    session.flush()
    session.add(TargetVersion(target_id=item.id, version=1, config=data.model_dump()))
    record(session, "target_created", target_id=item.id, version=1)
    session.flush()
    return item


def add_workflow(session, data: WorkflowInput, project_id="default") -> Workflow:
    item = Workflow(version=1, project_id=project_id)
    session.add(item)
    session.flush()
    session.add(WorkflowVersion(workflow_id=item.id, version=1, config=data.model_dump()))
    record(session, "workflow_created", workflow_id=item.id, version=1)
    session.flush()
    return item


@app.get("/health")
def health():
    return {"status": "ok"}


@api.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@api.get("/status")
async def status():
    def worker_ready() -> bool:
        with Session() as session:
            return bool(
                session.scalar(
                    select(WorkerHeartbeat.id).where(WorkerHeartbeat.seen_at > relative_time(-25)).limit(1)
                )
            )

    browser, worker, adscore = await asyncio.gather(
        asyncio.to_thread(browser_availability),
        asyncio.to_thread(worker_ready),
        diagnostics_status(),
    )
    adscore["configured"] = adscore["state"] in ("pending_smoke_test", "ready")
    adscore["ready"] = adscore["state"] == "ready"
    return {"browser": browser, "worker": {"ready": worker}, "adscore": adscore}


@api.get("/targets", response_model=list[TargetView])
def targets(request: Request, response: Response,
            limit: int = Query(500, ge=1, le=500), offset: int = Query(0, ge=0),
            q: str | None = Query(None, max_length=100), mode: str | None = Query(None, max_length=32),
            verified: bool | None = None, order: str = Query("newest", max_length=32)):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        query = select(Target).join(TargetVersion, (TargetVersion.target_id == Target.id) & (TargetVersion.version == Target.version)).where(Target.project_id == project_id)
        name = TargetVersion.config["name"].as_string()
        if q:
            query = query.where(or_(text_match(name, q), text_match(TargetVersion.config["origin"].as_string(), q)))
        if mode:
            query = query.where(TargetVersion.config["mode"].as_string() == mode)
        if verified is not None:
            query = query.where((Target.verified_version == Target.version) if verified else (Target.verified_version != Target.version))
        query = ordered(query, order, {
            "newest": (TargetVersion.created_at.desc(), Target.id.desc()),
            "oldest": (TargetVersion.created_at.asc(), Target.id.asc()),
            "name_asc": (name.asc(), Target.id.asc()), "name_desc": (name.desc(), Target.id.desc()),
        })
        response.headers["X-Total-Count"] = str(session.scalar(select(func.count()).select_from(query.order_by(None).subquery())))
        return [
            target_view(session, item)
            for item in session.scalars(query.limit(limit).offset(offset))
        ]


@api.post("/targets", status_code=201, response_model=TargetView)
def create_target(request: Request, data: TargetInput):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        return target_view(session, add_target(session, data, project_id))


@api.put("/targets/{target_id}", response_model=TargetView)
def update_target(request: Request, target_id: str, data: TargetInput):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        item = session.scalar(
            select(Target).where(Target.id == target_id, Target.project_id == project_id).with_for_update()
        )
        if item is None:
            raise HTTPException(404, "Target not found.")
        item.version += 1
        item.proof_token = secrets.token_urlsafe(32)
        item.verified_version = item.version if locally_owned_origins(data) else 0
        session.add(TargetVersion(target_id=item.id, version=item.version, config=data.model_dump()))
        record(session, "target_updated", target_id=item.id, version=item.version)
        session.flush()
        return target_view(session, item)


@api.get("/targets/{target_id}/versions")
def target_versions(request: Request, target_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        lookup(session, Target, target_id, project_id)
        return [
            {"version": v.version, "created_at": v.created_at, **v.config}
            for v in session.scalars(
                select(TargetVersion)
                .where(TargetVersion.target_id == target_id)
                .order_by(TargetVersion.version.desc())
            )
        ]


class PinnedProofConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, address: str):
        super().__init__(hostname, port=443, timeout=5, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        with_socket = socket.create_connection((self.address, self.port), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(with_socket, server_hostname=self.host)
        except BaseException:
            with_socket.close()
            raise


def fetch_proof(hostname: str, address: str) -> bytes:
    connection = PinnedProofConnection(hostname, address)
    try:
        connection.request("GET", PROOF_PATH, headers={"Accept": "text/plain", "Connection": "close"})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError("Marker must return HTTP 200 without redirecting.")
        return response.read(256).strip()
    finally:
        connection.close()


@api.post("/targets/{target_id}/verify", response_model=TargetView)
async def verify_target(request: Request, target_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        item = lookup(session, Target, target_id, project_id)
        config = target_view(session, item)
        if config["verified"]:
            return config
        origins = {config["origin"], *config.get("navigation_origins", [])}
        token = item.proof_token
        version = item.version
    for origin in sorted(origins):
        if origin in (API_ORIGIN, CONTROLLED_ORIGIN):
            continue
        hostname = urlsplit(origin).hostname
        try:
            addresses = await asyncio.wait_for(resolve_public_ipv4(hostname), timeout=6)
            marker = await asyncio.wait_for(
                asyncio.to_thread(fetch_proof, hostname, addresses[0]), timeout=8
            )
        except (ValueError, OSError, http.client.HTTPException, TimeoutError, IndexError):
            raise HTTPException(422, f"Could not read the ownership marker over HTTPS at {origin}.")
        if not hmac.compare_digest(marker, token.encode()):
            raise HTTPException(422, f"Ownership marker did not match at {origin}.")
    with Session.begin() as session:
        item = session.scalar(
            select(Target).where(Target.id == target_id, Target.project_id == project_id).with_for_update()
        )
        if item.version != version:
            raise HTTPException(409, "The target changed. Verify its current version.")
        item.verified_version = version
        record(session, "target_verified", target_id=item.id, version=version)
        return target_view(session, item)


@api.get("/workflows", response_model=list[WorkflowView])
def workflows(request: Request, response: Response,
              limit: int = Query(500, ge=1, le=500), offset: int = Query(0, ge=0),
              q: str | None = Query(None, max_length=100), order: str = Query("newest", max_length=32)):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        query = select(Workflow).join(WorkflowVersion, (WorkflowVersion.workflow_id == Workflow.id) & (WorkflowVersion.version == Workflow.version)).where(Workflow.project_id == project_id)
        name = WorkflowVersion.config["name"].as_string()
        if q:
            query = query.where(text_match(name, q))
        query = ordered(query, order, {
            "newest": (WorkflowVersion.created_at.desc(), Workflow.id.desc()),
            "oldest": (WorkflowVersion.created_at.asc(), Workflow.id.asc()),
            "name_asc": (name.asc(), Workflow.id.asc()), "name_desc": (name.desc(), Workflow.id.desc()),
        })
        response.headers["X-Total-Count"] = str(session.scalar(select(func.count()).select_from(query.order_by(None).subquery())))
        return [
            workflow_view(session, item)
            for item in session.scalars(query.limit(limit).offset(offset))
        ]


@api.post("/workflows", status_code=201, response_model=WorkflowView)
def create_workflow(request: Request, data: WorkflowInput):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        return workflow_view(session, add_workflow(session, data, project_id))


@api.put("/workflows/{workflow_id}", response_model=WorkflowView)
def update_workflow(request: Request, workflow_id: str, data: WorkflowInput):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        item = session.scalar(
            select(Workflow)
            .where(Workflow.id == workflow_id, Workflow.project_id == project_id)
            .with_for_update()
        )
        if item is None:
            raise HTTPException(404, "Workflow not found.")
        item.version += 1
        session.add(WorkflowVersion(workflow_id=item.id, version=item.version, config=data.model_dump()))
        record(session, "workflow_updated", workflow_id=item.id, version=item.version)
        session.flush()
        return workflow_view(session, item)


@api.get("/workflows/{workflow_id}/versions")
def workflow_versions(request: Request, workflow_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        lookup(session, Workflow, workflow_id, project_id)
        return [
            {"version": v.version, "created_at": v.created_at, **v.config}
            for v in session.scalars(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow_id)
                .order_by(WorkflowVersion.version.desc())
            )
        ]


@api.post("/starter")
def starter(request: Request):
    project_id = request.headers.get("x-project-id", "default")
    from abrl.starter import seed_starter

    if project_id != "default":
        raise HTTPException(
            409, "Starter fixtures belong to the default project; create site-specific workflows here"
        )
    return seed_starter()


@api.get("/runs", response_model=list[RunView])
def runs(request: Request):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        return [
            run_view(run)
            for run in session.scalars(
                select(Run).where(Run.project_id == project_id).order_by(Run.created_at.desc()).limit(100)
            )
        ]


@api.post("/runs", status_code=201, response_model=RunView)
def create_run(request: Request, data: RunInput):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        target = lookup(session, Target, data.target_id, project_id)
        workflow = lookup(session, Workflow, data.workflow_id, project_id)
        if data.mode == "live" and target.verified_version != target.version:
            raise HTTPException(409, "Verify the current target before launching browser trials.")
        count = session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.status.in_(["queued", "running", "paused", "cancelling"]))
        )
        if count >= 8:
            raise HTTPException(429, "Finish or cancel pending runs before starting another.")
        repetitions = data.repetitions or (300 if data.phase == "confirmation" else 30)
        run = Run(
            project_id=project_id,
            target_id=target.id,
            workflow_id=workflow.id,
            target_version=target.version,
            workflow_version=workflow.version,
            target=session.get(TargetVersion, (target.id, target.version)).config,
            workflow=session.get(WorkflowVersion, (workflow.id, workflow.version)).config,
            phase=data.phase,
            mode=data.mode,
            repetitions=repetitions,
            seed=data.seed,
            status="queued",
            reliability_target=data.reliability_target,
        )
        session.add(run)
        session.flush()
        session.add_all(Trial(run_id=run.id, ordinal=i) for i in range(1, repetitions + 1))
        record(session, "run_queued", run_id=run.id, mode=run.mode, trials=repetitions)
        return run_view(run)


@api.get("/runs/{run_id}/trials", response_model=list[TrialView])
def trials(request: Request, run_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        lookup(session, Run, run_id, project_id)
        return [
            trial_view(item)
            for item in session.scalars(select(Trial).where(Trial.run_id == run_id).order_by(Trial.ordinal))
        ]


@api.get("/runs/{run_id}/report", response_model=ReportView)
def get_report(request: Request, run_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        return report(session, lookup(session, Run, run_id, project_id))


@api.post("/runs/{run_id}/archive")
def archive_report(request: Request, run_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        run = lookup(session, Run, run_id, project_id)
        if run.status not in ("completed", "blocked", "cancelled"):
            raise HTTPException(409, "Only finished runs can be archived.")
        data = report(session, run)
    try:
        result = archive(data)
    except (RuntimeError, BotoCoreError, ClientError):
        log.warning("Report archive unavailable")
        raise HTTPException(503, "Report storage is unavailable.")
    with Session.begin() as session:
        record(session, "report_archived", run_id=run_id, mode=data["mode"])
    return result


@api.get("/runs/{run_id}/export")
def export(
    request: Request, run_id: str, format: str = Query(pattern="^(json|csv|html|pdf)$", default="json")
):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        data = report(session, lookup(session, Run, run_id, project_id))
    writer, media_type = {
        "csv": (csv_export, "text/csv"),
        "html": (html_export, "text/html"),
        "pdf": (pdf_export, "application/pdf"),
        "json": (lambda item: json.dumps(item, indent=2).encode(), "application/json"),
    }[format]
    return Response(
        writer(data),
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="abrl-{run_id}.{format}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@api.post("/runs/{run_id}/pause")
def pause(request: Request, run_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        run = locked_run(session, run_id)
        if run is None or run.project_id != project_id:
            raise HTTPException(404, "Run not found.")
        if run.status not in ("queued", "running"):
            raise HTTPException(409, "Only active runs can be paused.")
        run.status = "paused"
        record(session, "run_paused", run_id=run_id)
        return run_view(run)


@api.post("/runs/{run_id}/resume")
def resume(request: Request, run_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        run = locked_run(session, run_id)
        if run is None or run.project_id != project_id:
            raise HTTPException(404, "Run not found.")
        if run.status != "paused":
            raise HTTPException(409, "Only paused runs can be resumed.")
        run.status = "queued"
        session.execute(
            update(Trial).where(Trial.run_id == run_id, Trial.status == "queued").values(dispatched_at=None)
        )
        record(session, "run_resumed", run_id=run_id)
        return run_view(run)


@api.post("/runs/{run_id}/cancel")
def cancel(request: Request, run_id: str):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        run = locked_run(session, run_id)
        if run is None or run.project_id != project_id:
            raise HTTPException(404, "Run not found.")
        if run.status not in ("queued", "running", "paused", "cancelling"):
            raise HTTPException(409, "Run already ended.")
        run.status = "cancelling"
        session.execute(
            update(Trial)
            .where(Trial.run_id == run_id, Trial.status == "queued")
            .values(
                status="cancelled",
                outcome="cancelled",
                dispatched_at=None,
            )
        )
        record(session, "run_cancelling", run_id=run_id)
        settle_run(session, run)
        return run_view(run)


@api.get("/runs/{run_id}/events")
async def events(
    run_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None),
):
    project_id = request.headers.get("x-project-id", "default")
    with Session() as session:
        lookup(session, Run, run_id, project_id)
    if last_event_id and last_event_id.isascii() and last_event_id.isdecimal() and len(last_event_id) < 16:
        after = max(after, int(last_event_id))

    def read_events(cursor: int):
        with Session() as session:
            return session.scalars(
                select(Event)
                .where(Event.run_id == run_id, Event.sequence > cursor)
                .order_by(Event.sequence)
                .limit(100)
            ).all()

    async def stream():
        cursor = after
        while not await request.is_disconnected():
            batch = await asyncio.to_thread(read_events, cursor)
            for event in batch:
                cursor = event.sequence
                data = json.dumps({"kind": event.kind, "time": event.created_at, "detail": event.detail})
                yield f"id: {cursor}\ndata: {data}\n\n"
            if not batch:
                yield ": keepalive\n\n"
                await asyncio.sleep(2)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


@api.get("/diagnostics")
def diagnostics(request: Request):
    project_id = request.headers.get("x-project-id", "default")
    if project_id != "default":
        return []
    with Session() as session:
        return [
            {
                "id": x.id,
                "created_at": x.created_at,
                "verification_status": x.verification_status,
                "verdict": x.verdict,
                "raw_code": x.raw_code,
                "source": x.source,
            }
            for x in session.scalars(
                select(ManualObservation).order_by(ManualObservation.created_at.desc()).limit(100)
            )
        ]


app.include_router(api)

app.include_router(platform_router)
app.include_router(experiment_router)
app.include_router(artifact_router)
app.include_router(template_router)

app.include_router(observation_router)

configure_auth(app)

instrument(app)
