from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from abrl import access, observations, platform_api, session_runner
from abrl.api import app
from abrl.browser import BrowserResult
from abrl.db import Base, Target, TargetVersion, Workflow, WorkflowVersion
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Membership,
    Project,
    Schedule,
    ScheduledOccurrence,
    SessionAttempt,
    SessionBatch,
)
from abrl.platform_schemas import BatchInput, ScheduleInput
from abrl.platform_service import create_batch
from abrl.scheduling import materialize_due, preview


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("ABRL_COMMAND_ENCRYPTION_KEY", "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (access, platform_api, session_runner, observations):
        monkeypatch.setattr(module, "Session", factory)
    monkeypatch.setattr(access, "OPERATOR_TOKEN", "test-token-" + "x" * 32)
    monkeypatch.setattr(observations, "OPERATOR_TOKEN", "test-token-" + "x" * 32)
    monkeypatch.setattr(access, "CLOUD", False)
    with factory.begin() as connection:
        access.ensure_bootstrap(connection)
        connection.add_all(
            [
                Target(id="target", proof_token="proof", version=1, verified_version=1),
                Workflow(id="workflow", version=1),
            ]
        )
        connection.flush()
        connection.add_all(
            [
                TargetVersion(
                    target_id="target",
                    version=1,
                    config={
                        "name": "Fixture",
                        "origin": "http://127.0.0.1:8400",
                        "mode": "browser_only",
                        "resource_origins": [],
                        "concurrency": 10,
                        "rate_limit": 60,
                    },
                ),
                WorkflowVersion(
                    workflow_id="workflow",
                    version=1,
                    config={
                        "name": "Landing",
                        "steps": [
                            {"action": "navigate", "url": "/lab"},
                            {"action": "assert_visible", "selector": "#lab-ready"},
                        ],
                    },
                ),
            ]
        )
    yield factory
    engine.dispose()


def data(**changes):
    return BatchInput(
        target_id="target", workflow_id="workflow", idempotency_key="request-001", **{"count": 1, **changes}
    )


def client():
    return TestClient(app, headers={"Authorization": "Bearer test-token-" + "x" * 32})


def create(db, **changes):
    with db.begin() as connection:
        return create_batch(connection, data(**changes), "default")


def test_batch_idempotency_and_independent_profiles(db):
    with client() as api:
        payload = data(count=4, concurrency=4).model_dump(mode="json")
        first = api.post("/api/v1/batches", json=payload)
        assert first.status_code == 201, first.text
        second = api.post("/api/v1/batches", json=payload)
        assert first.json()["id"] == second.json()["id"]
        assert api.post("/api/v1/batches", json={**payload, "count": 5}).status_code == 409
        sessions = api.get("/api/v1/sessions?limit=2").json()
        assert sessions["total"] == 4 and len(sessions["items"]) == 2
    with db() as connection:
        assert len(set(connection.scalars(select(BrowserSession.profile_id)))) == 4


def test_roles_and_project_isolation(db):
    with db.begin() as connection:
        owner = access.ensure_bootstrap(connection)
        connection.add(Project(id="other", name="Other"))
        connection.flush()
        connection.add(Membership(project_id="other", user_id=owner.id, role="viewer"))
    batch = create(db)
    with client() as api:
        response = api.get("/api/v1/batches", headers={"X-Project-ID": "other"})
        assert response.json()["total"] == 0
        assert (
            api.post(f"/api/v1/batches/{batch.id}/cancel", headers={"X-Project-ID": "other"}).status_code
            == 403
        )
        assert (
            api.post(
                "/api/v1/batches", json=data().model_dump(mode="json"), headers={"X-Project-ID": "other"}
            ).status_code
            == 403
        )
        assert api.get("/api/v1/sessions", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_profile_owner_epoch_and_lost_lease_quarantine(db):
    create(db, count=2, concurrency=2, persistence="persistent")
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    with db.begin() as connection:
        row = connection.get(BrowserSession, work.id)
        row.lease_until = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        connection.get(SessionAttempt, work.lease).started_action = True
        session_runner.reap_lost(connection)
    with db() as connection:
        profile = connection.get(BrowserProfile, work.profile_id)
        assert profile.status == "quarantined" and profile.owner_session_id == work.id
        assert connection.get(BrowserSession, work.id).status == "needs_reconciliation"
    with client() as api:
        assert api.post(f"/api/v1/profiles/{work.profile_id}/reset", json={}).status_code == 409


def test_pause_stops_admission_cancel_retains_result(db):
    batch = create(db, count=3, concurrency=1)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    first = session_runner.claim("standard")
    session_runner.finish(first, BrowserResult("completed", 30, None, {"runtime_stopped": True}, True))
    with client() as api:
        api.post(f"/api/v1/batches/{batch.id}/pause")
        with db.begin() as connection:
            session_runner.admission_tick(connection)
        assert session_runner.claim("standard") is None
        api.post(f"/api/v1/batches/{batch.id}/cancel")
        rows = api.get("/api/v1/sessions").json()["items"]
        assert sorted(row["status"] for row in rows) == ["cancelled", "cancelled", "completed"]


def test_manual_control_single_owner_and_idempotency(db):
    create(db)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    with db.begin() as connection:
        row = connection.get(BrowserSession, work.id)
        row.status = "awaiting_operator"
        identity = row.id
    with client() as api:
        endpoint = f"/api/v1/sessions/{identity}/commands"
        assert (
            api.post(
                endpoint, json={"command": "click", "x": 1, "y": 1, "idempotency_key": "click-01"}
            ).status_code
            == 409
        )
        claim = {"command": "claim_control", "idempotency_key": "claim-01"}
        assert api.post(endpoint, json=claim).status_code == 200
        assert api.post(endpoint, json=claim).status_code == 200
        assert api.post(endpoint, json={**claim, "command": "stop"}).status_code == 409
        assert (
            api.post(endpoint, json={"command": "continue", "idempotency_key": "continue-01"}).status_code
            == 200
        )


def test_schedule_jerusalem_dst_and_occurrence_replay(db):
    cfg = {"kind": "cron", "expression": "30 2 * * *", "timezone": "Asia/Jerusalem"}
    times = preview(cfg, datetime(2026, 3, 26, 0, 0, tzinfo=UTC), 4)
    assert all(not t["local"].startswith("2026-03-27T02:30") for t in times)
    repeated = preview({**cfg, "expression": "30 1 * * *"}, datetime(2026, 10, 24, 0, 0, tzinfo=UTC), 4)
    assert sum(t["local"].startswith("2026-10-25T01:30") for t in repeated) == 1
    with db.begin() as connection:
        from abrl.platform_service import batch_snapshot

        batch = data()
        instant = datetime.now(UTC)
        cfg = ScheduleInput(name="Once", kind="once", start_at=instant, batch=batch).model_dump(mode="json")
        cfg["pinned"] = batch_snapshot(connection, batch, "default")
        schedule = Schedule(project_id="default", name="Once", config=cfg, next_fire_at=instant.isoformat())
        connection.add(schedule)
        connection.flush()
        materialize_due(connection, instant + timedelta(seconds=1))
        materialize_due(connection, instant + timedelta(seconds=2))
    with db() as connection:
        assert len(connection.scalars(select(ScheduledOccurrence)).all()) == 1
        assert len(connection.scalars(select(SessionBatch)).all()) == 1


@pytest.mark.asyncio
async def test_observations_verified_negative_and_failed_positive_callback(db, monkeypatch):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "mode": "instrumented"}
    create(db, mode="live", count=2, concurrency=2)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")

    async def negative(**_):
        return {"verification_status": "verified", "verdict": "bot", "raw_code": 9}

    monkeypatch.setattr(observations, "verify_observation", negative)
    value = observations.ObservationInput(
        context=observations.context_token(work.id, work.lease, work.epoch),
        signature="positive-looking-callback",
        idempotency_key="observation-01",
        client_ip="8.8.8.8",
        user_agent="Test browser",
    )
    first = await observations.ingest(value, source="site_connector")
    assert first["accepted"] is False and first["raw_code"] == 9
    assert (await observations.ingest(value, source="site_connector"))["id"] == first["id"]
    session_runner.finish(work, BrowserResult("completed", 10, None, {"runtime_stopped": True}, True))
    with db.begin() as connection:
        connection.get(Target, "target").last_started_at = None
        session_runner.admission_tick(connection)
    other = session_runner.claim("standard")

    async def failed(**_):
        return {"verification_status": "failed", "verdict": None, "raw_code": None}

    monkeypatch.setattr(observations, "verify_observation", failed)
    value = value.model_copy(
        update={"context": observations.context_token(other.id, other.lease, other.epoch)}
    )
    result = await observations.ingest(value, source="site_connector")
    assert result["accepted"] is False and result["verification_status"] == "failed"


def test_recovery_requires_explicit_evidence_and_fences_stale_worker(db):
    create(db)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    with db.begin() as connection:
        row = connection.get(BrowserSession, work.id)
        row.lease_until = "2000-01-01T00:00:00+00:00"
        session_runner.reap_lost(connection)
    with client() as api:
        endpoint = f"/api/v1/profiles/{work.profile_id}/recover"
        assert api.post(endpoint, json={}).status_code == 422
        recovered = api.post(
            endpoint,
            json={
                "ownership_epoch": work.epoch,
                "confirmed_stopped": True,
                "evidence": "Local worker process and browser process explicitly terminated",
            },
        )
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["epoch"] == work.epoch + 1
    session_runner.finish(work, BrowserResult("completed", 1, None, {"runtime_stopped": True}, True))
    with db() as connection:
        assert connection.get(BrowserSession, work.id).status == "failed"
        assert connection.get(BrowserProfile, work.profile_id).owner_session_id is None


def test_experiment_draft_version_and_execution_idempotency(db, monkeypatch):
    from abrl import experiment_api

    monkeypatch.setattr(experiment_api, "Session", db)
    payload = {
        "name": "Matched group definitions",
        "groups": [data().model_dump(mode="json"), data().model_dump(mode="json")],
    }
    with client() as api:
        created = api.post("/api/v1/experiments", json=payload)
        assert created.status_code == 201, created.text
        identity = created.json()["id"]
        assert (
            api.post(
                f"/api/v1/experiments/{identity}/runs", headers={"Idempotency-Key": "run-once-01"}
            ).status_code
            == 409
        )
        assert api.post(f"/api/v1/experiments/{identity}/validate").json()["status"] == "ready"
        first = api.post(f"/api/v1/experiments/{identity}/runs", headers={"Idempotency-Key": "run-once-01"})
        assert first.status_code == 201, first.text
        second = api.post(
            f"/api/v1/experiments/{identity}/runs", headers={"Idempotency-Key": "run-once-01"}
        )
        assert first.json() == second.json() and len(first.json()["batch_ids"]) == 2
        assert (
            api.put(f"/api/v1/experiments/{identity}", json={**payload, "name": "New revision"}).json()[
                "version"
            ]
            == 2
        )
        assert api.get(f"/api/v1/experiments/{identity}/runs").json()[0]["version"] == 1


def test_encrypted_artifact_access_and_retention(db, monkeypatch, tmp_path):
    import base64

    from abrl import session_artifacts

    monkeypatch.setattr(session_artifacts, "Session", db)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(session_artifacts, "storage", lambda: None)
    monkeypatch.setenv("ABRL_PROFILE_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"a" * 32).decode())
    create(db)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    identity = session_artifacts.store_artifact(
        work,
        {
            "data_base64": base64.b64encode(b"sensitive sample content").decode(),
            "media_type": "text/plain",
            "name": "example.txt",
        },
    )
    from abrl.platform_models import SessionArtifact

    with db() as connection:
        record = connection.get(SessionArtifact, identity)
        assert b"sensitive sample content" not in (tmp_path / record.key).read_bytes()
    with client() as api:
        response = api.get("/api/v1/artifacts/" + identity)
        assert response.content == b"sensitive sample content"
        assert (
            api.get(
                "/api/v1/artifacts/" + identity, headers={"Authorization": "Bearer unauthorized"}
            ).status_code
            == 401
        )
    with db.begin() as connection:
        connection.get(SessionArtifact, identity).expires_at = "2000-01-01T00:00:00+00:00"
    session_artifacts.expire_artifacts()
    with client() as api:
        assert api.get("/api/v1/artifacts/" + identity).status_code == 410


@pytest.mark.asyncio
async def test_controlled_callback_uses_trusted_metadata_and_gate(db, monkeypatch):
    monkeypatch.setenv("ABRL_ADSCORE_ZONE_KEY", "authorized_test_zone")
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "mode": "instrumented", "origin": "https://fixture.example"}
    create(db, mode="live")
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    captured = {}

    async def failed(**kwargs):
        captured.update(kwargs)
        return {"verification_status": "failed", "verdict": None, "raw_code": None}

    monkeypatch.setattr(observations, "verify_observation", failed)
    context = observations.context_token(work.id, work.lease, work.epoch)
    with TestClient(app, base_url="https://fixture.example", client=("8.8.8.8", 51000)) as api:
        page = api.get("/lab/research", params={"context": context, "next": "/lab/form"})
        assert page.status_code == 200, page.text
        response = api.post(
            "/lab/research",
            json={
                "context": context,
                "signature": "callback-says-success",
                "idempotency_key": "signature-01",
                "client_ip": "1.1.1.1",
            },
            headers={"Origin": "https://fixture.example", "User-Agent": "actual-agent"},
        )
        assert response.status_code == 200, response.text
        assert captured["ip_address"] == "8.8.8.8" and captured["user_agent"] == "actual-agent"
        assert response.json()["accepted"] is False
        assert api.post("/lab/form", data={"name": "Sample", "topic": "research"}).status_code == 403
        injection = api.get(
            "/lab/research", params={"context": context, "next": "/lab</script><script>bad()</script>"}
        )
        assert injection.status_code == 422
