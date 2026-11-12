"""Reusable immutable session configuration, independent of each launch identity."""

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from abrl.access import authorize, scoped
from abrl.db import Session
from abrl.list_queries import text_match
from abrl.platform_models import SessionTemplate, SessionTemplateRevision
from abrl.platform_schemas import BatchInput, SessionTemplateInput
from abrl.platform_service import batch_snapshot, event, list_page
from abrl.platform_views import Page

router = APIRouter(prefix="/api/v1/session-templates")


class SessionTemplateView(BaseModel):
    id: str
    project_id: str
    name: str
    version: int
    created_at: str
    batch: BatchInput
    target_version: int
    workflow_version: int
    proxy_policy_version: int | None


def template_view(db, row, version=None):
    revision = db.get(SessionTemplateRevision, (row.id, version or row.version))
    if revision is None:
        raise HTTPException(404, "Session template version not found")
    pinned = revision.config["pinned"]
    return {
        "id": row.id,
        "project_id": row.project_id,
        "name": revision.config["name"],
        "version": revision.version,
        "created_at": revision.created_at,
        "batch": {
            **revision.config["batch"],
            "session_template_id": row.id,
            "session_template_version": revision.version,
        },
        "target_version": pinned["target_version"],
        "workflow_version": pinned["workflow_version"],
        "proxy_policy_version": (pinned.get("proxy_policy") or {}).get("version"),
    }


def revision_config(db, data, project_id):
    # Saving a revision is an explicit new definition, not a recursive template.
    batch = data.batch.model_copy(update={"session_template_id": None, "session_template_version": None})
    return {
        "name": data.name,
        "batch": batch.model_dump(mode="json"),
        "pinned": batch_snapshot(db, batch, project_id),
    }


@router.get("", response_model=Page[SessionTemplateView])
def templates(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str = Query("", max_length=100),
    order_by: Literal["created_at", "name", "version"] = "created_at",
    direction: Literal["asc", "desc"] = "desc",
):
    actor = authorize(request)
    with Session() as db:
        query = select(SessionTemplate).where(SessionTemplate.project_id == actor.project_id)
        if q:
            query = query.where(text_match(SessionTemplate.name, q))
        column = getattr(SessionTemplate, order_by)
        query = query.order_by(column.desc() if direction == "desc" else column.asc(), SessionTemplate.id)
        return list_page(db, query, lambda row: template_view(db, row), limit=limit, offset=offset)


@router.post("", response_model=SessionTemplateView, status_code=201)
def create_template(data: SessionTemplateInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        config = revision_config(db, data, actor.project_id)
        row = SessionTemplate(project_id=actor.project_id, name=data.name)
        db.add(row)
        db.flush()
        db.add(SessionTemplateRevision(template_id=row.id, version=1, config=config))
        event(
            db,
            actor.project_id,
            "session_template_created",
            actor_id=actor.user_id,
            template_id=row.id,
            version=1,
        )
        db.flush()
        return template_view(db, row)


@router.put("/{template_id}", response_model=SessionTemplateView)
def update_template(template_id: str, data: SessionTemplateInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, SessionTemplate, template_id, actor)
        db.refresh(row, with_for_update=True)
        config = revision_config(db, data, actor.project_id)
        row.name, row.version = data.name, row.version + 1
        db.add(SessionTemplateRevision(template_id=row.id, version=row.version, config=config))
        event(
            db,
            actor.project_id,
            "session_template_updated",
            actor_id=actor.user_id,
            template_id=row.id,
            version=row.version,
        )
        db.flush()
        return template_view(db, row)


@router.get("/{template_id}/versions", response_model=Page[SessionTemplateView])
def template_versions(
    template_id: str, request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)
):
    actor = authorize(request)
    with Session() as db:
        row = scoped(db, SessionTemplate, template_id, actor)
        return list_page(
            db,
            select(SessionTemplateRevision)
            .where(SessionTemplateRevision.template_id == template_id)
            .order_by(SessionTemplateRevision.version.desc()),
            lambda revision: template_view(db, row, revision.version),
            limit=limit,
            offset=offset,
        )


@router.get("/{template_id}/versions/{version}", response_model=SessionTemplateView)
def template_version(template_id: str, version: int, request: Request):
    actor = authorize(request)
    with Session() as db:
        return template_view(db, scoped(db, SessionTemplate, template_id, actor), version)
