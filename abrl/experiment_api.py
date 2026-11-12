"""Immutable multi-workflow experiments, randomized allocations and run-wide reports."""

import hashlib
import json
import random
import sys
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from abrl.access import authorize, scoped
from abrl.db import Session
from abrl.platform_models import BrowserSession, Experiment, ExperimentRevision, ExperimentRun, SessionBatch
from abrl.platform_schemas import BatchInput, ExperimentInput
from abrl.platform_service import (
    batch_snapshot,
    batch_view,
    create_batch,
    digest,
    event,
    execution_capabilities,
    list_page,
)

router = APIRouter(prefix="/api/v1/experiments")


def view(row):
    return {
        "id": row.id,
        "name": row.name,
        "version": row.version,
        "status": row.status,
        "created_at": row.created_at,
        **{k: v for k, v in row.config.items() if k != "pinned"},
    }


def save_config(db, data, project):
    if len({group.phase for group in data.groups}) != 1:
        raise HTTPException(
            422, "Pilot and confirmation groups require separately declared experiment runs"
        )
    return {**data.model_dump(mode="json"), "pinned": [batch_snapshot(db, g, project) for g in data.groups]}


def randomize_run(db, run, batches, groups):
    """One permutation covers all declared allocations, including failed creation."""
    group_seeds = [int(group["seed"]) for group in groups]
    seed = (
        group_seeds[0]
        if len(set(group_seeds)) == 1
        else int.from_bytes(
            hashlib.sha256(json.dumps(group_seeds, separators=(",", ":")).encode()).digest()[:4], "big"
        )
    )
    order = [
        {"group_index": index, "batch_id": batch.id, "ordinal": ordinal}
        for index, batch in enumerate(batches)
        for ordinal in range(1, batch.count + 1)
    ]
    random.Random(seed).shuffle(order)
    run.randomization = {
        "seed": seed,
        "concurrency": batches[0].config.get("experiment_concurrency", 1),
        "group_seeds": group_seeds,
        "algorithm": "seeded_cross_group_shuffle_v1",
        "python_version": sys.version.split()[0],
        "order": order,
        "constraint_policy": "Admission follows this order among eligible allocations; target, profile, proxy, license and concurrency constraints may defer a position.",
    }
    for batch in batches:
        apply_experiment_dispatch(db, batch)


def apply_experiment_dispatch(db, batch):
    """Reattach a retried creation to its original allocation; never reshuffle."""
    run_id = batch.config.get("experiment_run_id")
    if not run_id:
        return
    run = db.get(ExperimentRun, run_id)
    if not run or not run.randomization:
        return
    positions = {
        item["ordinal"]: position
        for position, item in enumerate(run.randomization["order"], start=1)
        if item["batch_id"] == batch.id
    }
    for session in db.scalars(select(BrowserSession).where(BrowserSession.batch_id == batch.id)):
        if session.ordinal not in positions:
            raise ValueError("A session is absent from the immutable experiment allocation plan")
        session.created_at = run.created_at
        session.dispatch_order = positions[session.ordinal]


def randomization_summary(run):
    return {key: value for key, value in (run.randomization or {}).items() if key != "order"}


@router.get("")
def experiments(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    status: Literal["draft", "ready", "blocked"] | None = None,
    mode: Literal["live", "demo"] | None = None,
    order: Literal["newest", "oldest", "name_asc", "name_desc"] = "newest",
):
    actor = authorize(request)
    with Session() as db:
        query = select(Experiment).where(Experiment.project_id == actor.project_id)
        if q:
            query = query.where(Experiment.name.icontains(q, autoescape=True))
        if status:
            query = query.where(Experiment.status == status)
        if mode:
            query = query.where(Experiment.config["groups"][0]["mode"].as_string() == mode)
        ordering = {
            "newest": Experiment.created_at.desc(),
            "oldest": Experiment.created_at.asc(),
            "name_asc": Experiment.name.asc(),
            "name_desc": Experiment.name.desc(),
        }
        return list_page(
            db,
            query.order_by(ordering[order], Experiment.id),
            view,
            limit=limit,
            offset=offset,
        )


@router.post("", status_code=201)
def create_experiment(data: ExperimentInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = Experiment(
            project_id=actor.project_id, name=data.name, config=save_config(db, data, actor.project_id)
        )
        db.add(row)
        db.flush()
        db.add(ExperimentRevision(experiment_id=row.id, version=1, config=row.config))
        event(db, actor.project_id, "experiment_created", actor_id=actor.user_id, experiment_id=row.id)
        return view(row)


@router.put("/{experiment_id}")
def edit_experiment(experiment_id: str, data: ExperimentInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, Experiment, experiment_id, actor)
        db.refresh(row, with_for_update=True)
        row.version += 1
        row.config = save_config(db, data, actor.project_id)
        row.name = data.name
        row.status = "draft"
        db.add(ExperimentRevision(experiment_id=row.id, version=row.version, config=row.config))
        event(
            db,
            actor.project_id,
            "experiment_updated",
            actor_id=actor.user_id,
            experiment_id=row.id,
            version=row.version,
        )
        return view(row)


@router.post("/{experiment_id}/validate")
def validate_experiment(experiment_id: str, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, Experiment, experiment_id, actor)
        db.refresh(row, with_for_update=True)
        live = row.config["groups"][0]["mode"] == "live"
        adapters = {a for g in row.config["groups"] for a in g["adapters"]}
        capabilities = [a for a in execution_capabilities() if a["id"] in adapters]
        issues = (
            [{"adapter": a["id"], "reason": a["reason"]} for a in capabilities if not a["available"]]
            if live
            else []
        )
        row.status = "ready" if not issues else "blocked"
        event(
            db,
            actor.project_id,
            "experiment_validated",
            actor_id=actor.user_id,
            experiment_id=row.id,
            status=row.status,
        )
        return {
            "status": row.status,
            "issues": issues,
            "capabilities": capabilities,
            "live_verified": False,
        }


@router.post("/{experiment_id}/clone", status_code=201)
def clone_experiment(experiment_id: str, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        original = scoped(db, Experiment, experiment_id, actor)
        row = Experiment(
            project_id=actor.project_id,
            name=original.name[:95] + " copy",
            config={**original.config, "name": original.name[:95] + " copy"},
        )
        db.add(row)
        db.flush()
        db.add(ExperimentRevision(experiment_id=row.id, version=1, config=row.config))
        event(
            db,
            actor.project_id,
            "experiment_cloned",
            actor_id=actor.user_id,
            experiment_id=row.id,
            source=original.id,
        )
        return view(row)


@router.post("/{experiment_id}/runs", status_code=201)
def run_experiment(experiment_id: str, request: Request):
    actor = authorize(request, "operator")
    key = request.headers.get("idempotency-key", "")
    if not 8 <= len(key) <= 100:
        raise HTTPException(422, "Provide an Idempotency-Key of8-100characters")
    with Session.begin() as db:
        row = scoped(db, Experiment, experiment_id, actor)
        db.refresh(row, with_for_update=True)
        prior = db.scalar(
            select(ExperimentRun).where(
                ExperimentRun.experiment_id == row.id, ExperimentRun.idempotency_key == key
            )
        )
        if prior:
            return {
                "id": prior.id,
                "version": prior.version,
                "batch_ids": prior.batch_ids,
                "randomization": randomization_summary(prior),
            }
        if row.status != "ready":
            raise HTTPException(409, "Validate the current experiment definition before executing")
        run = ExperimentRun(experiment_id=row.id, version=row.version, idempotency_key=key, batch_ids=[])
        db.add(run)
        db.flush()
        batches = []
        for index, group in enumerate(row.config["groups"]):
            data = BatchInput.model_validate({**group, "idempotency_key": f"experiment:{run.id}:{index}"})
            config = {
                **row.config["pinned"][index],
                **data.model_dump(mode="json"),
                "experiment_id": row.id,
                "experiment_version": row.version,
                "experiment_run_id": run.id,
                "experiment_concurrency": row.config.get("concurrency", 1),
            }
            batches.append(create_batch(db, data, actor.project_id, pinned=config, actor_id=actor.user_id))
        run.batch_ids = [batch.id for batch in batches]
        randomize_run(db, run, batches, row.config["groups"])
        event(
            db,
            actor.project_id,
            "experiment_run_created",
            actor_id=actor.user_id,
            experiment_id=row.id,
            run_id=run.id,
            batch_ids=run.batch_ids,
            randomization=randomization_summary(run),
        )
        return {
            "id": run.id,
            "version": run.version,
            "batch_ids": run.batch_ids,
            "randomization": randomization_summary(run),
        }


@router.get("/{experiment_id}/runs")
def experiment_runs(experiment_id: str, request: Request):
    actor = authorize(request)
    with Session() as db:
        scoped(db, Experiment, experiment_id, actor)
        return [
            {
                "id": r.id,
                "version": r.version,
                "created_at": r.created_at,
                "randomization": randomization_summary(r),
                "report_frozen": r.report_snapshot is not None,
                "concurrency": db.get(ExperimentRevision, (experiment_id, r.version)).config.get(
                    "concurrency", 1
                ),
                "batches": [batch_view(db, db.get(SessionBatch, b)) for b in r.batch_ids],
            }
            for r in db.scalars(
                select(ExperimentRun)
                .where(ExperimentRun.experiment_id == experiment_id)
                .order_by(ExperimentRun.created_at.desc())
                .limit(100)
            )
        ]


def scoped_run(db, experiment_id, run_id, actor):
    scoped(db, Experiment, experiment_id, actor)
    run = db.get(ExperimentRun, run_id)
    if not run or run.experiment_id != experiment_id:
        raise HTTPException(404, "Experiment run not found")
    return run


@router.get("/{experiment_id}/runs/{run_id}/comparison")
def run_comparison(experiment_id: str, run_id: str, request: Request):
    from abrl.research import experiment_comparison

    actor = authorize(request, "analyst")
    with Session() as db:
        run = scoped_run(db, experiment_id, run_id, actor)
        if run.report_snapshot is not None:
            if digest(run.report_snapshot) != run.report_checksum:
                raise HTTPException(409, "Stored experiment report checksum mismatch")
            return run.report_snapshot
        return experiment_comparison(db, run)


@router.get("/{experiment_id}/runs/{run_id}/export")
def run_export(
    experiment_id: str,
    run_id: str,
    request: Request,
    format: str = Query("json", pattern="^(json|csv|html|pdf)$"),
):
    from abrl.research import experiment_comparison, export_comparison

    actor = authorize(request, "analyst")
    with Session.begin() as db:
        run = scoped_run(db, experiment_id, run_id, actor)
        db.refresh(run, with_for_update=True)
        if run.report_snapshot is None:
            batches = [db.get(SessionBatch, identifier) for identifier in run.batch_ids]
            if not batches or any(
                batch is None or batch.status not in {"completed", "cancelled", "creation_failed"}
                for batch in batches
            ):
                raise HTTPException(409, "Finish every declared batch before freezing a run report")
            data = experiment_comparison(db, run)
            if len(data["trials"]) != data["snapshot"]["planned"]:
                raise HTTPException(409, "Every planned allocation needs a session or creation outcome")
            run.report_snapshot = data
            run.report_checksum = digest(data)
            event(
                db,
                actor.project_id,
                "experiment_report_frozen",
                actor_id=actor.user_id,
                experiment_id=experiment_id,
                run_id=run_id,
                checksum=run.report_checksum,
            )
        data = run.report_snapshot
        if digest(data) != run.report_checksum:
            raise HTTPException(409, "Stored experiment report checksum mismatch")
    return Response(
        export_comparison(data, format),
        media_type={
            "json": "application/json",
            "csv": "text/csv",
            "html": "text/html",
            "pdf": "application/pdf",
        }[format],
        headers={
            "Content-Disposition": f'attachment; filename="abrl-experiment-{run_id}.{format}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
