"""Saved session versions pin execution inputs instead of following mutable drafts."""

from datetime import datetime, timedelta

import test_platform
from sqlalchemy import select
from test_platform import client, data

from abrl import session_template_api
from abrl.db import TargetVersion, Workflow, WorkflowVersion
from abrl.platform_models import ScheduledOccurrence, SessionBatch, SessionTemplateRevision
from abrl.scheduling import materialize_due

db = test_platform.db
parameterized_workflow = test_platform.parameterized_workflow


def test_template_revision_pins_inputs_and_launches_have_independent_identity(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    user_agent = "ABRL Fixture/1.0"
    referrer = "https://fixture.example/landing?source=qa"
    with client() as api:
        payload = {
            "name": "Reusable landing",
            "batch": {**data().model_dump(mode="json"), "user_agent": f"  {user_agent}  ", "referrer": referrer},
        }
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
        config = batch.json()["config"]
        assert config["workflow_version"] == 1
        assert config["user_agent"] == user_agent
        assert config["referrer"] == referrer
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
        for setting in ("user_agent", "referrer"):
            rejected = api.post(
                "/api/v1/batches",
                json={**launch, setting: None, "idempotency_key": f"changed-{setting}"},
            )
            assert rejected.status_code == 409, rejected.text
        payload["batch"].update(user_agent=None, referrer=None)
        second = api.put(f"/api/v1/session-templates/{first['id']}", json=payload).json()
        assert second["version"] == 2 and second["workflow_version"] == 2
        assert second["batch"]["user_agent"] is None and second["batch"]["referrer"] is None
        retained = api.get(f"/api/v1/session-templates/{first['id']}/versions/1").json()
        assert retained["workflow_version"] == 1
        assert retained["batch"]["user_agent"] == user_agent
        assert retained["batch"]["referrer"] == referrer
        versions = api.get(f"/api/v1/session-templates/{first['id']}/versions?limit=1").json()
        assert versions["total"] == 2 and len(versions["items"]) == 1
        assert (
            api.get("/api/v1/session-templates?q=Reusable&order_by=name&direction=asc").json()["total"] == 1
        )
        assert api.get("/api/v1/session-templates?q=missing").json()["total"] == 0
    with db() as connection:
        assert len(list(connection.scalars(select(SessionBatch)))) == 2
        assert len(list(connection.scalars(select(SessionTemplateRevision)))) == 2


def test_javascript_capture_is_independent_of_the_pinned_template(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    with client() as api:
        template_response = api.post(
            "/api/v1/session-templates",
            json={"name": "Live landing", "batch": data(mode="live").model_dump(mode="json")},
        )
        assert template_response.status_code == 201, template_response.text
        template = template_response.json()
        launch = {**template["batch"], "idempotency_key": "template-capture-enabled"}
        enabled = api.post("/api/v1/batches", json={**launch, "capture_javascript": True})
        assert enabled.status_code == 201, enabled.text
        assert enabled.json()["config"]["capture_javascript"] is True
        assert enabled.json()["config"]["target_version"] == template["target_version"]
        assert enabled.json()["config"]["workflow_version"] == template["workflow_version"]

        disabled = api.post("/api/v1/batches", json={**launch, "idempotency_key": "template-capture-disabled", "capture_javascript": False})
        assert disabled.status_code == 201, disabled.text
        assert disabled.json()["config"]["capture_javascript"] is False
        fetched = api.get(f"/api/v1/session-templates/{template['id']}/versions/1")
        assert fetched.json() == template


def test_pinned_template_applies_one_launch_override_to_its_workflow_revision(
    db, monkeypatch, parameterized_workflow
):
    monkeypatch.setattr(session_template_api, "Session", db)
    with client() as api:
        definition = {"name": "Local parameters", "batch": data().model_dump(mode="json")}
        response = api.post("/api/v1/session-templates", json=definition)
        assert response.status_code == 201, response.text
        template = response.json()
        assert template["fixture_parameter_names"] == ["QUERY", "SUBID"]
        with db.begin() as connection:
            connection.get(Workflow, "workflow").version = 2
            connection.add(
                WorkflowVersion(
                    workflow_id="workflow",
                    version=2,
                    config={
                        "name": "Updated",
                        "steps": [
                            {"action": "navigate", "url": "/lab"},
                            {"action": "assert_visible", "selector": "#lab-ready"},
                        ],
                    },
                )
            )
        launch = {
            **template["batch"],
            "idempotency_key": "pinned-fixture-values",
            "fixture_params": {"SUBID": "batch only"},
        }
        batch = api.post("/api/v1/batches", json=launch)
        assert batch.status_code == 201, batch.text
        config = batch.json()["config"]
        assert config["workflow_version"] == 1
        assert config["fixture_params"] == {"SUBID": "batch only"}
        assert config["workflow"]["steps"][0]["fixture_params"] == {
            "QUERY": "original query",
            "SUBID": "batch only",
        }
        assert config["workflow"]["steps"][1:] == parameterized_workflow[1:]

        invalid_definition = {
            **definition,
            "batch": {**definition["batch"], "fixture_params": {"QUERY": "local QA"}},
        }
        assert api.post("/api/v1/session-templates", json=invalid_definition).status_code == 409

    with client(production=True) as api:
        view = api.get(f"/api/v1/session-templates/{template['id']}/versions/1")
        assert view.status_code == 200, view.text
        assert view.json()["fixture_parameter_names"] == ["QUERY", "SUBID"]
        rejected = api.post("/api/v1/batches", json={**launch, "idempotency_key": "retired-fixture-values"})
        assert rejected.status_code == 409, rejected.text
        assert "Lab site was retired" in rejected.json()["detail"]

    with db() as connection:
        stored = connection.get(SessionTemplateRevision, (template["id"], 1))
        assert stored.config["pinned"]["workflow"]["steps"] == parameterized_workflow
        assert len(list(connection.scalars(select(SessionBatch)))) == 1


def test_navigation_only_is_scoped_at_direct_and_pinned_batch_admission(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    absolute = "http://127.0.0.1:8400/lab?view=check"
    with db.begin() as connection:
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {**workflow.config, "steps": [{"action": "navigate", "url": absolute}]}
    payload = data().model_dump(mode="json")
    with client() as api:
        created = api.post("/api/v1/session-templates", json={"name": "Local navigation", "batch": payload})
        assert created.status_code == 201, created.text
        template = created.json()
        assert template["fixture_parameter_names"] == []
        local = api.post("/api/v1/batches", json=payload)
        assert local.status_code == 201, local.text
        assert local.json()["config"]["workflow"]["steps"] == [{"action": "navigate", "url": absolute}]

        with db.begin() as connection:
            target = connection.get(TargetVersion, ("target", 1))
            target.config = {**target.config, "origin": "https://outside.example"}
        direct = api.post("/api/v1/batches", json={**payload, "idempotency_key": "external-navigation"})
        assert direct.status_code == 409, direct.text
        assert "registered target scope" in direct.json()["detail"]

    with client(production=True) as api:
        pinned = api.post(
            "/api/v1/batches", json={**template["batch"], "idempotency_key": "retired-navigation"}
        )
        assert pinned.status_code == 409, pinned.text
        assert "Lab site was retired" in pinned.json()["detail"]


def test_pinned_external_navigation_applies_launch_parameters(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    step = {
        "action": "navigate",
        "url": "/verify?q=[QUERY]&id=[SUBID]",
        "fixture_params": {"QUERY": "original query", "SUBID": "original subid"},
    }
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://store.example"}
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {**workflow.config, "steps": [step]}

    with client() as api:
        created = api.post(
            "/api/v1/session-templates",
            json={"name": "External verification", "batch": data(mode="live").model_dump(mode="json")},
        )
        assert created.status_code == 201, created.text
        template = created.json()
        assert template["fixture_parameter_names"] == ["QUERY", "SUBID"]

        launch = {
            **template["batch"],
            "idempotency_key": "external-verification",
            "fixture_params": {"SUBID": "verification & QA"},
        }
        response = api.post("/api/v1/batches", json=launch)
        assert response.status_code == 201, response.text
        config = response.json()["config"]
        assert config["workflow_version"] == 1
        assert config["workflow"]["steps"] == [
            {**step, "fixture_params": {"QUERY": "original query", "SUBID": "verification & QA"}}
        ]

    with db() as connection:
        revision = connection.get(SessionTemplateRevision, (template["id"], 1))
        assert revision.config["pinned"]["workflow"]["steps"] == [step]


def test_template_version_is_pinned_inside_schedule(db, monkeypatch):
    monkeypatch.setattr(session_template_api, "Session", db)
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://schedule.example"}
    settings = {"user_agent": "ABRL Fixture/1.0", "referrer": "https://fixture.example/schedule"}
    with client() as api:
        template = api.post(
            "/api/v1/session-templates",
            json={
                "name": "Schedule template",
                "batch": data(**settings).model_dump(mode="json"),
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
        plan = schedule.json()
        assert plan["batch"]["session_template_version"] == 1
        bad = {**template["batch"], "session_template_version": 999, "idempotency_key": "invalid-version"}
        assert api.post("/api/v1/batches", json=bad).status_code == 404
    with db.begin() as connection:
        materialize_due(connection, datetime.fromisoformat(plan["next_fire_at"]) + timedelta(seconds=1))
        occurrence = connection.scalar(select(ScheduledOccurrence))
        assert occurrence.status == "dispatched"
        config = connection.get(SessionBatch, occurrence.batch_id).config
        assert config["session_template_version"] == 1
        assert {key: config[key] for key in settings} == settings
