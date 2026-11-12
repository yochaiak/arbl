"""Saved session versions pin execution inputs instead of following mutable drafts."""

import test_platform
from sqlalchemy import select
from test_platform import client, data

from abrl import session_template_api
from abrl.db import Workflow, WorkflowVersion
from abrl.platform_models import SessionBatch, SessionTemplateRevision

db = test_platform.db


def test_template_revision_pins_inputs_and_launches_have_independent_identity(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    with client() as api:
        payload = {"name": "Reusable landing", "batch": data().model_dump(mode="json")}
        result = api.post("/api/v1/session-templates", json=payload)
        assert result.status_code == 201, result.text
        first = result.json()
        assert first["version"] == 1 and first["workflow_version"] == 1
        with db.begin() as connection:
            connection.get(Workflow, "workflow").version = 2
            connection.add(
                WorkflowVersion(
                    workflow_id="workflow", version=2, config={"name": "Edited landing", "steps": []}
                )
            )
        launch = {**first["batch"], "name": "Launch one", "idempotency_key": "template-launch-one"}
        batch = api.post("/api/v1/batches", json=launch)
        assert batch.status_code == 201, batch.text
        assert batch.json()["config"]["workflow_version"] == 1
        assert (
            api.post(
                "/api/v1/batches", json={**launch, "idempotency_key": "template-launch-two"}
            ).status_code
            == 201
        )
        assert (
            api.post(
                "/api/v1/batches", json={**launch, "count": 2, "idempotency_key": "changed-template"}
            ).status_code
            == 409
        )
        second = api.put(f"/api/v1/session-templates/{first['id']}", json=payload).json()
        assert second["version"] == 2 and second["workflow_version"] == 2
        retained = api.get(f"/api/v1/session-templates/{first['id']}/versions/1").json()
        assert retained["workflow_version"] == 1
        versions = api.get(f"/api/v1/session-templates/{first['id']}/versions?limit=1").json()
        assert versions["total"] == 2 and len(versions["items"]) == 1
        assert (
            api.get("/api/v1/session-templates?q=Reusable&order_by=name&direction=asc").json()["total"] == 1
        )
        assert api.get("/api/v1/session-templates?q=missing").json()["total"] == 0
    with db() as connection:
        assert len(list(connection.scalars(select(SessionBatch)))) == 2
        assert len(list(connection.scalars(select(SessionTemplateRevision)))) == 2


def test_template_version_is_pinned_inside_schedule(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    with client() as api:
        template = api.post(
            "/api/v1/session-templates",
            json={
                "name": "Schedule template",
                "batch": data().model_dump(mode="json"),
            },
        ).json()
        schedule = api.post(
            "/api/v1/schedules",
            json={
                "name": "Pinned recurring",
                "kind": "interval",
                "interval_seconds": 300,
                "batch": template["batch"],
            },
        )
        assert schedule.status_code == 201, schedule.text
        assert schedule.json()["batch"]["session_template_version"] == 1
        bad = {**template["batch"], "session_template_version": 999, "idempotency_key": "invalid-version"}
        assert api.post("/api/v1/batches", json=bad).status_code == 404
