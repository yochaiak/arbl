from datetime import UTC, datetime, timedelta

import test_platform
from sqlalchemy import select
from test_platform import client, data

from abrl.db import TargetVersion
from abrl.platform_models import BrowserProfile, BrowserSession, SessionBatch, SessionBatchItem
from abrl.platform_service import session_launch_config

db = test_platform.db


def test_block_versions_are_pinned_and_nested_navigation_is_checked(db):
    with client() as api:
        block = api.post("/api/v1/workflow-blocks", json={
            "name": "Check form", "steps": [{"action": "assert_visible", "selector": "#lab-ready"}],
        }).json()
        changed = api.put(f"/api/v1/workflow-blocks/{block['id']}", json={
            "name": "Check form", "steps": [{"action": "assert_visible", "selector": "#later"}],
        })
        assert changed.status_code == 200
        assert [item["version"] for item in api.get(f"/api/v1/workflow-blocks/{block['id']}/versions").json()] == [2, 1]
        response = api.post("/api/v1/workflows", json={
            "schema_version": 2, "name": "Pinned flow", "steps": [
                {"action": "navigate", "url": "/lab"},
                {"action": "use_block", "block_id": block["id"], "block_version": 1},
            ],
        })
        assert response.status_code == 201, response.text
        workflow = response.json()
        assert workflow["steps"][1]["steps"][0]["selector"] == "#lab-ready"
        created = api.post("/api/v1/batches", json={
            "target_id": "target", "workflow_id": workflow["id"], "count": 1,
            "idempotency_key": "pinned-block",
        })
        assert created.status_code == 201, created.text
        nested = api.post("/api/v1/workflows", json={
            "schema_version": 2, "name": "Out of scope", "steps": [
                {"action": "navigate", "url": "/lab"},
                {"action": "if", "condition": {"kind": "visible", "selector": "#lab-ready"},
                 "steps": [{"action": "navigate", "url": "https://elsewhere.example/check"}]},
            ],
        }).json()
        denied = api.post("/api/v1/batches/preflight", json={
            "target_id": "target", "workflow_id": nested["id"], "idempotency_key": "nested-scope",
        })
        assert denied.status_code == 409, denied.text


def test_workflow_navigation_timeout_is_pinned_with_its_revision(db):
    payload = {
        "schema_version": 2, "name": "Navigation timing", "initial_navigation_timeout_ms": 18_000,
        "steps": [
            {"action": "navigate", "url": "/lab"},
            {"action": "navigate", "url": "/lab/form", "timeout_ms": 5000},
        ],
    }
    with client() as api:
        created = api.post("/api/v1/workflows", json=payload)
        assert created.status_code == 201, created.text
        workflow = created.json()
        assert workflow["initial_navigation_timeout_ms"] == 18_000
        assert "timeout_ms" not in workflow["steps"][0]
        assert workflow["steps"][1]["timeout_ms"] == 5000
        batch = api.post("/api/v1/batches", json={
            "target_id": "target", "workflow_id": workflow["id"], "count": 1,
            "idempotency_key": "navigation-timing-revision",
        })
        assert batch.status_code == 201, batch.text
        changed = api.put(f"/api/v1/workflows/{workflow['id']}", json={**payload, "initial_navigation_timeout_ms": 25_000})
        assert changed.status_code == 200, changed.text
        versions = api.get(f"/api/v1/workflows/{workflow['id']}/versions").json()
        assert [item["initial_navigation_timeout_ms"] for item in versions] == [25_000, 18_000]

    with db() as connection:
        session = connection.scalar(select(BrowserSession))
        pinned = session_launch_config(connection, session)["workflow"]
        assert pinned["initial_navigation_timeout_ms"] == 18_000
        assert "timeout_ms" not in pinned["steps"][0]
        assert pinned["steps"][1]["timeout_ms"] == 5000


def test_groups_freeze_assignments_and_preflight_has_no_allocation_side_effect(db):
    with db.begin() as session:
        session.add(BrowserProfile(
            id="saved-profile", project_id="default", name="Retained Chrome", adapter="playwright",
            persistence="persistent",
        ))
    with client() as api:
        workflow = api.post("/api/v1/workflows", json={
            "schema_version": 2, "name": "Input and query",
            "inputs": {"profile_label": {"type": "string", "required": True}},
            "steps": [{"action": "navigate", "url": "/lab?q=[q]", "fixture_params": {"q": "seed"}}],
        }).json()
        payload = {
            "target_id": "target", "workflow_id": workflow["id"],
            "idempotency_key": "group-matrix-01", "concurrency": 2,
            "session_groups": [
                {"adapter": "playwright", "count": 2, "profile_ids": [None, "saved-profile"],
                 "workflow_inputs": {"profile_label": "first"}, "fixture_params": {"q": "sample & snow"}},
                {"adapter": "kameleo", "count": 1, "device_preset": "phone",
                 "workflow_inputs": {"profile_label": "second"}, "use_direct": True},
            ],
        }
        result = api.post("/api/v1/batches/preflight", json=payload)
        assert result.status_code == 200, result.text
        approval = result.json()
        assert approval["count"] == 3
        with db() as session:
            assert session.scalar(select(SessionBatch)) is None
            assert len(list(session.scalars(select(BrowserProfile)))) == 1
        stale = api.post("/api/v1/batches", json={**payload, "expected_dependencies": "0" * 64})
        assert stale.status_code == 409
        request = {**payload, "expected_dependencies": approval["expected_dependencies"]}
        result = api.post("/api/v1/batches", json=request)
        assert result.status_code == 201, result.text
        batch = result.json()
        assert api.post("/api/v1/batches", json=request).json()["id"] == batch["id"]
        with db() as session:
            rows = list(session.scalars(select(BrowserSession).order_by(BrowserSession.ordinal)))
            assert [row.adapter for row in rows] == ["playwright", "playwright", "kameleo"]
            assert [row.persistence for row in rows] == ["fresh", "persistent", "fresh"]
            assert rows[1].profile_id == "saved-profile"
            first = session_launch_config(session, rows[0])
            last = session_launch_config(session, rows[-1])
            assert first["workflow"]["steps"][0]["fixture_params"] == {"q": "sample & snow"}
            assert first["workflow_inputs"] == {"profile_label": "first"}
            assert last["workflow"]["steps"][0]["fixture_params"] == {"q": "seed"}
            assert last["workflow_inputs"] == {"profile_label": "second"} and last["device_preset"] == "phone"
            assert session.get(SessionBatchItem, (batch["id"], 1)).launch_config["persistence"] == "fresh"


def test_timing_only_schedule_edit_retains_workflow_version(db):
    with db.begin() as session:
        target = session.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://schedule.example"}
    with client() as api:
        payload = {
            "name": "Recurring controlled QA", "kind": "interval",
            "interval_seconds": 3600,
            "start_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            "batch": data().model_dump(mode="json"),
        }
        original = api.post("/api/v1/schedules", json=payload)
        assert original.status_code == 201, original.text
        schedule = original.json()
        assert schedule["dependencies"]["workflow_version"] == 1
        workflow = api.put("/api/v1/workflows/workflow", json={
            "name": "Updated landing", "steps": [{"action": "navigate", "url": "/new"}],
        })
        assert workflow.status_code == 200, workflow.text
        edited = api.put(f"/api/v1/schedules/{schedule['id']}", json={**payload, "interval_seconds": 7200})
        assert edited.status_code == 200 and edited.json()["dependencies"]["workflow_version"] == 1
        refreshed = api.put(f"/api/v1/schedules/{schedule['id']}", json={**payload, "refresh_dependencies": True})
        assert refreshed.status_code == 200 and refreshed.json()["dependencies"]["workflow_version"] == 2
