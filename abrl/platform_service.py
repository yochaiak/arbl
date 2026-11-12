import hashlib
import json
import random
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select

from abrl.db import Target, TargetVersion, Workflow, WorkflowVersion, now
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    ProxyPolicy,
    SessionBatch,
    SessionBatchItem,
    SessionEvent,
    SessionTemplate,
    SessionTemplateRevision,
)
from abrl.platform_schemas import BatchInput

ACTIVE = ("starting", "preparing", "ready", "running", "awaiting_operator", "stopping")
TERMINAL = ("completed", "failed", "blocked", "cancelled", "needs_reconciliation")
GLOBAL_LIMIT = 500


def utc_after(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def event(db, project_id, kind, session_id=None, actor_id=None, **detail):
    db.add(
        SessionEvent(
            project_id=project_id, session_id=session_id, kind=kind, actor_id=actor_id, detail=detail
        )
    )


def batch_snapshot(db, data: BatchInput, project_id: str) -> dict:
    if data.session_template_id:
        template = db.get(SessionTemplate, data.session_template_id)
        revision = db.get(
            SessionTemplateRevision, (data.session_template_id, data.session_template_version)
        )
        if not template or template.project_id != project_id or not revision:
            raise HTTPException(404, "Session template version not found")
        pinned = revision.config["pinned"]
        # Name and request identity are launch-specific. Other changes require a
        # new template revision or an explicitly untemplated launch.
        ignored = {"name", "idempotency_key", "session_template_id", "session_template_version"}
        baseline = BatchInput.model_validate(revision.config["batch"]).model_dump(mode="json")
        requested = data.model_dump(mode="json")
        if any(requested[key] != baseline[key] for key in baseline if key not in ignored):
            raise HTTPException(409, "Launch configuration differs from the pinned session template")
        return {**pinned, **{key: requested[key] for key in ignored}}
    target = db.get(Target, data.target_id)
    workflow = db.get(Workflow, data.workflow_id)
    if not target or not workflow:
        raise HTTPException(404, "Target or workflow not found")
    if (
        getattr(target, "project_id", "default") != project_id
        or getattr(workflow, "project_id", "default") != project_id
    ):
        raise HTTPException(404, "Target or workflow not found in project")
    if data.mode == "live" and target.verified_version != target.version:
        raise HTTPException(409, "Verify the current target before running live sessions")
    policy = None
    if data.proxy_policy_id:
        policy_row = db.get(ProxyPolicy, data.proxy_policy_id)
        if not policy_row or policy_row.project_id != project_id:
            raise HTTPException(404, "Proxy policy not found")
        policy = {**policy_row.config, "id": policy_row.id, "version": policy_row.version}
    return {
        **data.model_dump(mode="json"),
        "target": db.get(TargetVersion, (target.id, target.version)).config,
        "target_version": target.version,
        "workflow": db.get(WorkflowVersion, (workflow.id, workflow.version)).config,
        "workflow_version": workflow.version,
        "proxy_policy": policy,
    }


def create_batch(
    db, data: BatchInput, project_id: str, *, pinned: dict | None = None, actor_id: str | None = None
) -> SessionBatch:
    request_hash = digest(data.model_dump(mode="json"))
    prior = db.scalar(
        select(SessionBatch).where(
            SessionBatch.project_id == project_id, SessionBatch.idempotency_key == data.idempotency_key
        )
    )
    if prior:
        if prior.request_hash != request_hash:
            raise HTTPException(409, "This idempotency key belongs to different input")
        return prior
    config = pinned or batch_snapshot(db, data, project_id)
    batch = SessionBatch(
        project_id=project_id,
        name=data.name,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        config=config,
        count=data.count,
        concurrency=data.concurrency,
    )
    db.add(batch)
    db.flush()
    adapters = [data.adapters[i % len(data.adapters)] for i in range(data.count)]
    random.Random(data.seed).shuffle(adapters)
    for index, adapter in enumerate(adapters):
        item = SessionBatchItem(
            batch_id=batch.id,
            ordinal=index + 1,
            adapter=adapter,
            requested_profile_id=data.profile_ids[index] if data.profile_ids else None,
        )
        db.add(item)
        create_batch_item(db, batch, item, data)
    event(
        db,
        project_id,
        "batch_created",
        actor_id=actor_id,
        batch_id=batch.id,
        count=data.count,
        mode=data.mode,
    )
    db.flush()
    settle_batch(db, batch.id)
    return batch


def create_batch_item(db, batch, item, data):
    """Retry only allocation; a session that ever existed is never recreated."""
    if item.session_id is not None or item.status == "cancelled":
        return
    item.attempts = (item.attempts or 0) + 1
    if item.requested_profile_id:
        profile = db.scalar(
            select(BrowserProfile).where(BrowserProfile.id == item.requested_profile_id).with_for_update()
        )
        if not profile or profile.project_id != batch.project_id or profile.adapter != item.adapter:
            item.status = "failed"
            item.error = "Selected profile is unavailable or incompatible with assigned adapter"
            return
        if profile.status != "available" or db.scalar(
            select(BrowserSession.id)
            .where(
                BrowserSession.profile_id == profile.id,
                BrowserSession.status.not_in(TERMINAL),
            )
            .limit(1)
        ):
            item.status = "failed"
            item.error = "Selected profile is active, reserved or quarantined"
            return
    else:
        profile = BrowserProfile(
            project_id=batch.project_id,
            name=f"{data.name} · {item.ordinal}",
            adapter=item.adapter,
            persistence=data.persistence,
        )
        db.add(profile)
        db.flush()
    session = BrowserSession(
        project_id=batch.project_id,
        batch_id=batch.id,
        profile_id=profile.id,
        adapter=item.adapter,
        persistence=data.persistence,
        mode=data.mode,
        pool="standard" if item.adapter in ("playwright", "manual_chrome") else "vendor",
        ordinal=item.ordinal,
        ground_truth="human"
        if item.adapter == "manual_chrome"
        else "mixed"
        if data.preparation.mode == "manual"
        else "automated",
    )
    db.add(session)
    db.flush()
    item.session_id, item.status, item.error = session.id, "created", None


def batch_item_view(row):
    return {
        key: getattr(row, key)
        for key in (
            "batch_id",
            "ordinal",
            "adapter",
            "requested_profile_id",
            "session_id",
            "status",
            "error",
            "attempts",
            "created_at",
        )
    }


def profile_view(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "project_id",
            "name",
            "adapter",
            "persistence",
            "status",
            "version",
            "epoch",
            "owner_session_id",
            "created_at",
        )
    }


def session_view(row):
    return {
        **{
            k: getattr(row, k)
            for k in (
                "id",
                "project_id",
                "profile_id",
                "batch_id",
                "adapter",
                "persistence",
                "status",
                "mode",
                "pool",
                "outcome",
                "workflow_outcome",
                "elapsed_ms",
                "error",
                "ordinal",
                "admitted",
                "ground_truth",
                "epoch",
                "attempt_number",
                "controller_id",
                "controller_until",
                "created_at",
                "started_at",
                "finished_at",
            )
        },
        "metadata": {
            k: v for k, v in row.metadata_json.items() if k not in ("viewer_frame", "correlation")
        },
    }


def batch_view(db, row):
    counts = dict(
        db.execute(
            select(BrowserSession.status, func.count())
            .where(BrowserSession.batch_id == row.id)
            .group_by(BrowserSession.status)
        ).all()
    )
    return {
        **{
            k: getattr(row, k)
            for k in (
                "id",
                "project_id",
                "name",
                "status",
                "count",
                "concurrency",
                "created_at",
                "finished_at",
            )
        },
        "mode": row.config["mode"],
        "config": row.config,
        "counts": counts,
        "creation": dict(
            db.execute(
                select(SessionBatchItem.status, func.count())
                .where(SessionBatchItem.batch_id == row.id)
                .group_by(SessionBatchItem.status)
            ).all()
        ),
    }


def settle_batch(db, batch_id):
    db.flush()
    row = db.get(SessionBatch, batch_id)
    statuses = list(db.scalars(select(BrowserSession.status).where(BrowserSession.batch_id == batch_id)))
    creation_failed = (
        db.scalar(
            select(SessionBatchItem.ordinal)
            .where(
                SessionBatchItem.batch_id == batch_id,
                SessionBatchItem.status == "failed",
            )
            .limit(1)
        )
        is not None
    )
    if all(s in TERMINAL for s in statuses):
        row.status = (
            "creation_failed"
            if creation_failed
            else ("cancelled" if not statuses or all(s == "cancelled" for s in statuses) else "completed")
        )
        row.finished_at = None if creation_failed else now()
    elif row.status not in ("paused", "cancelling") and any(s in ACTIVE for s in statuses):
        row.status = "running"


def list_page(db, query, view=lambda row: row, *, limit=50, offset=0):
    count = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {
        "items": [view(row) for row in db.scalars(query.limit(limit).offset(offset))],
        "total": count,
        "limit": limit,
        "offset": offset,
    }


def execution_capabilities():
    from abrl.adapters import adapter_capabilities

    return adapter_capabilities()
