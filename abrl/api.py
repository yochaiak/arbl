import asyncio
import json
import logging
import secrets

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError
from sqlalchemy import func, or_, select, update

from abrl.artifacts import archive
from abrl.auth import configure_auth
from abrl.browser import browser_availability
from abrl.config import WEB_ORIGIN
from abrl.db import (
    Event,
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
from abrl.list_queries import ordered, text_match
from abrl.network import FIXTURE_ORIGIN
from abrl.observations import router as observation_router
from abrl.platform_api import router as platform_router
from abrl.platform_models import FleetSlot, WorkflowBlock, WorkflowBlockRevision
from abrl.platform_service import uses_retired_lab_fixture
from abrl.reports import csv_export, html_export, pdf_export, report, run_view, trial_view
from abrl.runtime import locked_run, relative_time, settle_run
from abrl.schemas import (
    ReportView,
    RunInput,
    RunView,
    TargetInput,
    TargetView,
    TrialView,
    WorkflowBlockInput,
    WorkflowInput,
    WorkflowView,
    resolve_workflow_inputs,
    validate_navigation_workflow_scope,
    validate_workflow_page_paths,
    workflow_step_limit,
)
from abrl.session_artifacts import router as artifact_router
from abrl.session_template_api import router as template_router
from abrl.target_authorization import LIVE_SITE_APPROVAL_REQUIRED, target_is_live_ready
from abrl.telemetry import configure_logging, instrument
from abrl.verifier import diagnostics_status

configure_logging()
log = logging.getLogger("abrl.api")


app = FastAPI(title="ABRL Browser Workspace", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted({WEB_ORIGIN, "http://127.0.0.1:8430", "http://localhost:8430"}),
    allow_methods=["GET", "POST", "PUT"],
    allow_credentials=True,
    allow_headers=["Authorization", "Content-Type", "Last-Event-ID", "X-Project-ID", "Idempotency-Key"],
    expose_headers=["X-Total-Count"],
)


def authenticate(request: Request):
    from abrl.access import authorize

    return authorize(request, "operator" if request.method in ("POST", "PUT", "DELETE") else "viewer")


api = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticate)])


def lookup(session, model, id, project_id="default"):
    item = session.get(model, id)
    if item is None or getattr(item, "project_id", "default") != project_id:
        raise HTTPException(404, "Record not found.")
    return item


def target_view(session, item: Target, request: Request) -> dict:
    config = session.get(TargetVersion, (item.id, item.version)).config
    retired = config["origin"] == FIXTURE_ORIGIN and not request.scope.get("abrl.test_fixture")
    return {
        "id": item.id,
        "version": item.version,
        **config,
        "verified": item.verified_version == item.version,
        "live_ready": not retired and target_is_live_ready(item, item.version, config),
    }


def workflow_view(session, item: Workflow) -> dict:
    config = session.get(WorkflowVersion, (item.id, item.version)).config
    return {"id": item.id, "version": item.version, **config}


def add_target(session, data: TargetInput, project_id="default") -> Target:
    item = Target(
        project_id=project_id,
        version=1,
        proof_token="",
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
    session.add(WorkflowVersion(workflow_id=item.id, version=1, config=workflow_snapshot(session, data, project_id)))
    record(session, "workflow_created", workflow_id=item.id, version=1)
    session.flush()
    return item


def resolve_block_steps(session, steps, project_id):
    remaining = workflow_step_limit()

    def expand(nodes, stack):
        nonlocal remaining
        resolved = []
        for node in nodes:
            remaining -= 1
            if remaining < 0:
                raise HTTPException(422, "Expanded workflow exceeds the configured step limit")
            if node["action"] == "use_block":
                key = (node["block_id"], node["block_version"])
                if key in stack:
                    raise HTTPException(422, "Workflow blocks cannot call themselves")
                lookup(session, WorkflowBlock, key[0], project_id)
                revision = session.get(WorkflowBlockRevision, key)
                if revision is None:
                    raise HTTPException(404, "Workflow block revision was not found")
                node = {**node, "steps": expand(revision.config["steps"], (*stack, key))}
            else:
                node = {**node, **{
                    branch: expand(node[branch], stack) for branch in ("steps", "else_steps") if node.get(branch)
                }}
            resolved.append(node)
        return resolved

    return expand(steps, ())


def workflow_snapshot(session, data, project_id):
    payload = data.model_dump(mode="json")
    if data.schema_version != 2:
        return payload
    payload["steps"] = resolve_block_steps(session, payload["steps"], project_id)
    try:
        validated = WorkflowInput.model_validate(payload)
        validate_workflow_page_paths(payload["steps"])
    except (ValidationError, ValueError) as exc:
        raise HTTPException(422, "The expanded workflow has invalid steps or exceeds its configured limits") from exc
    return validated.model_dump(mode="json")


class WorkflowBlockView(WorkflowBlockInput):
    id: str
    version: int


def block_view(db, row, version=None):
    version = row.version if version is None else version
    revision = db.get(WorkflowBlockRevision, (row.id, version))
    return {"id": row.id, "version": version, **revision.config}


def save_block(db, row, data):
    payload = data.model_dump(mode="json")
    payload["steps"] = resolve_block_steps(db, payload["steps"], row.project_id)
    try:
        WorkflowBlockInput.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(422, "The expanded block exceeds the configured step limit") from exc
    db.add(WorkflowBlockRevision(block_id=row.id, version=row.version, config=payload))
    db.flush()
    return block_view(db, row)


@api.get("/workflow-blocks", response_model=list[WorkflowBlockView])
def workflow_blocks(request: Request):
    project = request.headers.get("x-project-id", "default")
    with Session() as db:
        return [block_view(db, row) for row in db.scalars(select(WorkflowBlock).where(WorkflowBlock.project_id == project))]


@api.post("/workflow-blocks", status_code=201, response_model=WorkflowBlockView)
def create_workflow_block(data: WorkflowBlockInput, request: Request):
    with Session.begin() as db:
        row = WorkflowBlock(project_id=request.headers.get("x-project-id", "default"))
        db.add(row)
        db.flush()
        return save_block(db, row, data)


@api.get("/workflow-blocks/{block_id}/versions", response_model=list[WorkflowBlockView])
def workflow_block_versions(block_id: str, request: Request):
    with Session() as db:
        row = lookup(db, WorkflowBlock, block_id, request.headers.get("x-project-id", "default"))
        return [block_view(db, row, version) for version in range(row.version, 0, -1)]


@api.put("/workflow-blocks/{block_id}", response_model=WorkflowBlockView)
def update_workflow_block(block_id: str, data: WorkflowBlockInput, request: Request):
    with Session.begin() as db:
        row = lookup(db, WorkflowBlock, block_id, request.headers.get("x-project-id", "default"))
        db.refresh(row, with_for_update=True)
        row.version += 1
        return save_block(db, row, data)


@app.get("/health")
def health():
    return {"status": "ok"}


@api.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@api.get("/status")
async def status():
    def worker_status() -> tuple[bool, dict[str, int]]:
        with Session() as session:
            cutoff = relative_time(-25)
            ready = bool(
                session.scalar(
                    select(WorkerHeartbeat.id).where(WorkerHeartbeat.seen_at > cutoff).limit(1)
                )
            )
            encoders = list(session.scalars(
                select(FleetSlot.metadata_json["recording_encoder"].as_boolean())
                .where(FleetSlot.seen_at > cutoff)
            ))
            return ready, {
                "ready_workers": sum(value is True for value in encoders),
                "reporting_workers": sum(value is not None for value in encoders),
            }

    browser, (worker, recording), adscore = await asyncio.gather(
        asyncio.to_thread(browser_availability),
        asyncio.to_thread(worker_status),
        diagnostics_status(),
    )
    adscore["configured"] = adscore["state"] in ("pending_smoke_test", "ready")
    adscore["ready"] = adscore["state"] == "ready"
    return {"browser": browser, "worker": {"ready": worker}, "adscore": adscore, "recording": recording}


@api.get("/targets", response_model=list[TargetView])
def targets(request: Request, response: Response,
            limit: int = Query(500, ge=1, le=500), offset: int = Query(0, ge=0),
            q: str | None = Query(None, max_length=100), mode: str | None = Query(None, max_length=32),
            verified: bool | None = Query(None, deprecated=True), order: str = Query("newest", max_length=32)):
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
            target_view(session, item, request)
            for item in session.scalars(query.limit(limit).offset(offset))
        ]


@api.post("/targets", status_code=201, response_model=TargetView)
def create_target(request: Request, data: TargetInput):
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        return target_view(session, add_target(session, data, project_id), request)


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
        session.add(TargetVersion(target_id=item.id, version=item.version, config=data.model_dump()))
        record(session, "target_updated", target_id=item.id, version=item.version)
        session.flush()
        return target_view(session, item, request)


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
        session.add(WorkflowVersion(workflow_id=item.id, version=item.version, config=workflow_snapshot(session, data, project_id)))
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
    if data.mode != "live":
        raise HTTPException(422, "New workflow runs require live execution")
    project_id = request.headers.get("x-project-id", "default")
    with Session.begin() as session:
        target = lookup(session, Target, data.target_id, project_id)
        workflow = lookup(session, Workflow, data.workflow_id, project_id)
        target_config = session.get(TargetVersion, (target.id, target.version)).config
        if not target_is_live_ready(target, target.version, target_config):
            raise HTTPException(409, LIVE_SITE_APPROVAL_REQUIRED)
        count = session.scalar(
            select(func.count())
            .select_from(Run)
            .where(Run.status.in_(["queued", "running", "paused", "cancelling"]))
        )
        if count >= 8:
            raise HTTPException(429, "Finish or cancel pending runs before starting another.")
        repetitions = data.repetitions or (300 if data.phase == "confirmation" else 30)
        workflow_config = session.get(WorkflowVersion, (workflow.id, workflow.version)).config
        if not request.scope.get("abrl.test_fixture") and uses_retired_lab_fixture(target_config, workflow_config):
            raise HTTPException(409, "The Lab site was retired. Select an authorized site.")
        try:
            validate_navigation_workflow_scope(
                workflow_config.get("steps", []),
                origin=target_config["origin"],
                resource_origins=target_config.get("resource_origins", []),
                navigation_origins=target_config.get("navigation_origins", []),
                all_navigation=workflow_config.get("schema_version") == 2,
            )
            resolve_workflow_inputs(workflow_config, {})
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        run = Run(
            project_id=project_id,
            target_id=target.id,
            workflow_id=workflow.id,
            target_version=target.version,
            workflow_version=workflow.version,
            target=target_config,
            workflow=workflow_config,
            phase=data.phase,
            mode=data.mode,
            repetitions=repetitions,
            seed=data.seed if data.seed is not None else secrets.randbelow(2**31),
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


app.include_router(api)

app.include_router(platform_router)
app.include_router(artifact_router)
app.include_router(template_router)

app.include_router(observation_router)

configure_auth(app)

instrument(app)
