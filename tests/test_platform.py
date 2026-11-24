from datetime import UTC, datetime, timedelta

import pytest
from browser_fixture import create_app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from abrl import access, observations, platform_api, platform_service, session_runner
from abrl import api as main_api
from abrl.browser import BrowserResult
from abrl.db import Base, Target, TargetVersion, Workflow, WorkflowVersion
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Membership,
    Project,
    ProviderObservation,
    Schedule,
    ScheduledOccurrence,
    SessionAttempt,
    SessionBatch,
)
from abrl.platform_schemas import BatchInput, ScheduleInput
from abrl.platform_service import create_batch
from abrl.scheduling import materialize_due, preview


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("ABRL_COMMAND_ENCRYPTION_KEY", "Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M=")
    # Worker heartbeats and finalization use different threads and connections.
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    for module in (access, main_api, platform_api, session_runner, observations):
        monkeypatch.setattr(module, "Session", factory)
    monkeypatch.setattr(access, "OPERATOR_TOKEN", "test-token-" + "x" * 32)
    monkeypatch.setattr(observations, "OPERATOR_TOKEN", "test-token-" + "x" * 32)
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


def client(*, production=False):
    return TestClient(main_api.app if production else create_app(), headers={"Authorization": "Bearer test-token-" + "x" * 32})


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


def test_session_workflow_identity_uses_the_pinned_batch_snapshot(db):
    original = create(db, count=2)
    with db.begin() as connection:
        workflow = connection.get(Workflow, "workflow")
        old_version = connection.get(WorkflowVersion, (workflow.id, 1))
        workflow.version = 2
        connection.add(
            WorkflowVersion(
                workflow_id=workflow.id, version=2, config={**old_version.config, "name": "Renamed landing"}
            )
        )
        updated = create_batch(
            connection, data().model_copy(update={"idempotency_key": "renamed-workflow-batch"}), "default"
        )

    with client() as api:
        response = api.get("/api/v1/sessions")
        assert response.status_code == 200, response.text
        sessions = response.json()["items"]
        assert {(row["batch_id"], row["workflow_name"], row["workflow_version"]) for row in sessions} == {
            (original.id, "Landing", 1),
            (updated.id, "Renamed landing", 2),
        }

        page = api.get("/api/v1/sessions", params={"batch_id": original.id, "limit": 1, "offset": 1}).json()
        assert page["total"] == 2
        (row,) = page["items"]
        assert (row["workflow_name"], row["workflow_version"]) == ("Landing", 1)

        endpoint = f"/api/v1/sessions/{row['id']}"
        detail = api.get(endpoint).json()
        assert (detail["workflow_name"], detail["workflow_version"]) == ("Landing", 1)
        stop = {"command": "stop", "idempotency_key": "stop-original-workflow"}
        stopped = api.post(f"{endpoint}/commands", json=stop)
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["status"] == "cancelled"
        assert (stopped.json()["workflow_name"], stopped.json()["workflow_version"]) == ("Landing", 1)
        assert api.post(f"{endpoint}/commands", json=stop).json() == stopped.json()


def test_session_workflow_identity_is_nullable_for_historical_snapshots(db):
    batch = create(db)
    with db.begin() as connection:
        stored = connection.get(SessionBatch, batch.id)
        stored.config = {
            key: value
            for key, value in stored.config.items()
            if key not in {"workflow", "workflow_version"}
        }
    with client() as api:
        (row,) = api.get("/api/v1/sessions").json()["items"]
        detail = api.get(f"/api/v1/sessions/{row['id']}").json()
        for item in (row, detail):
            assert item["workflow_name"] is None
            assert item["workflow_version"] is None


def test_parameterized_loopback_url_cannot_launch_on_an_external_target(db, monkeypatch):
    from abrl import api as legacy_api

    monkeypatch.setattr(legacy_api, "Session", db)
    step = {
        "action": "navigate",
        "url": "http://127.0.0.1:8400/lab?term=[QUERY]&pub_sub_id=[SUBID]",
        "fixture_params": {"QUERY": "local QA", "SUBID": "fixture-1"},
    }
    with db.begin() as connection:
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {**workflow.config, "steps": [step, workflow.config["steps"][-1]]}
    with client() as api:
        accepted = api.post("/api/v1/batches", json=data().model_dump(mode="json"))
        assert accepted.status_code == 201, accepted.text
        assert accepted.json()["config"]["workflow"]["steps"][0] == step

        with db.begin() as connection:
            target = connection.get(TargetVersion, ("target", 1))
            target.config = {**target.config, "origin": "https://outside.example"}
        rejected_batch = api.post(
            "/api/v1/batches",
            json={**data().model_dump(mode="json"), "idempotency_key": "different-target"},
        )
        rejected_run = api.post("/api/v1/runs", json={"target_id": "target", "workflow_id": "workflow"})
        for response in (rejected_batch, rejected_run):
            assert response.status_code == 409, response.text
            assert "registered target scope" in response.json()["detail"]


@pytest.mark.parametrize(
    ("url", "params"),
    [
        ("/verify", None),
        (
            "https://checkout.example/verify?q=[QUERY]&id=[SUBID]",
            {"QUERY": "QA", "SUBID": "run-1"},
        ),
    ],
)
def test_external_navigation_only_workflow_can_run_for_a_registered_site(db, url, params):
    with client() as api:
        registered = api.post(
            "/api/v1/targets",
            json={
                "name": "External verification site",
                "origin": "https://store.example",
                "navigation_origins": ["https://checkout.example"],
                "resource_origins": ["https://assets.example"],
                "authorized": True,
            },
        )
        assert registered.status_code == 201, registered.text
        target = registered.json()
        assert target["live_ready"]

        step = {"action": "navigate", "url": url, "fixture_params": params}
        saved = api.post("/api/v1/workflows", json={"name": "Verification page", "steps": [step]})
        assert saved.status_code == 201, saved.text
        workflow = saved.json()
        saved_step = workflow["steps"][0]
        assert {key: saved_step[key] for key in step} == step

        run = {"target_id": target["id"], "workflow_id": workflow["id"], "mode": "live", "repetitions": 1}
        batch = {
            **data().model_dump(mode="json"),
            "target_id": target["id"],
            "workflow_id": workflow["id"],
            "mode": "live",
        }
        for endpoint, payload in (("runs", run), ("batches", batch)):
            accepted = api.post(f"/api/v1/{endpoint}", json=payload)
            assert accepted.status_code == 201, accepted.text


@pytest.fixture
def parameterized_workflow(db):
    steps = [
        {
            "action": "navigate",
            "url": "/lab?term=[QUERY]&pub_sub_id=[SUBID]",
            "fixture_params": {"QUERY": "original query", "SUBID": "original subid"},
        },
        {
            "action": "navigate",
            "url": "/lab?term=[QUERY]",
            "fixture_params": {"QUERY": "another query"},
        },
        {"action": "assert_visible", "selector": "#lab-ready"},
    ]
    with db.begin() as connection:
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {**workflow.config, "steps": steps}
    return steps


def test_batch_launch_overrides_only_the_snapshot_and_preserves_retry_identity(db, parameterized_workflow):
    original_steps = parameterized_workflow
    payload = data().model_dump(mode="json")
    override = {"QUERY": "local QA & β"}
    with client() as api:
        response = api.post(
            "/api/v1/batches", json={**payload, "fixture_params": {"QUERY": f"  {override['QUERY']}  "}}
        )
        assert response.status_code == 201, response.text
        launch = response.json()
        assert launch["config"]["fixture_params"] == override
        assert launch["config"]["workflow"]["steps"] == [
            {
                **original_steps[0],
                "fixture_params": {"QUERY": override["QUERY"], "SUBID": "original subid"},
            },
            {**original_steps[1], "fixture_params": override},
            original_steps[2],
        ]

        retry = api.post("/api/v1/batches", json={**payload, "fixture_params": override})
        assert retry.status_code == 201, retry.text
        assert retry.json()["id"] == launch["id"]
        conflict = api.post("/api/v1/batches", json={**payload, "fixture_params": {"QUERY": "other"}})
        assert conflict.status_code == 409, conflict.text

        defaults_payload = {**payload, "idempotency_key": "default-fixture-values"}
        defaults = api.post("/api/v1/batches", json=defaults_payload)
        assert defaults.status_code == 201, defaults.text
        assert defaults.json()["config"].get("fixture_params") is None
        assert defaults.json()["config"]["workflow"]["steps"] == original_steps
        null_override = api.post("/api/v1/batches", json={**defaults_payload, "fixture_params": None})
        assert null_override.status_code == 201, null_override.text
        assert null_override.json()["id"] == defaults.json()["id"]

    with db() as connection:
        assert connection.get(WorkflowVersion, ("workflow", 1)).config["steps"] == original_steps
        assert len(list(connection.scalars(select(BrowserSession)))) == 2


@pytest.mark.parametrize("incompatible_scope", ["resource", "navigation"])
def test_fixture_override_rejects_incompatible_launch_scope(
    db, parameterized_workflow, incompatible_scope
):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        if incompatible_scope == "resource":
            target.config = {**target.config, "resource_origins": ["https://outside.example"]}
        elif incompatible_scope == "navigation":
            target.config = {**target.config, "navigation_origins": ["https://outside.example"]}
    with client() as api:
        response = api.post(
            "/api/v1/batches",
            json={**data().model_dump(mode="json"), "fixture_params": {"QUERY": "local QA"}},
        )
        assert response.status_code == 409, response.text
        assert "local ABRL test site" in response.json()["detail"]
    with db() as connection:
        assert not list(connection.scalars(select(SessionBatch)))


def test_fixture_override_rejects_unused_keys_and_overlong_resolved_urls(db, parameterized_workflow):
    with db.begin() as connection:
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {
            **workflow.config,
            "steps": [parameterized_workflow[1], parameterized_workflow[2]],
        }
    payload = data().model_dump(mode="json")
    with client() as api:
        unused = api.post("/api/v1/batches", json={**payload, "fixture_params": {"SUBID": "local QA"}})
        assert unused.status_code == 409, unused.text
        assert "SUBID" in unused.json()["detail"]
        overlong = api.post("/api/v1/batches", json={**payload, "fixture_params": {"QUERY": "😀" * 200}})
        assert overlong.status_code == 409, overlong.text
        assert "exceeds 1500 characters" in overlong.json()["detail"]
    with db() as connection:
        assert not list(connection.scalars(select(SessionBatch)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_agent", " "),
        ("user_agent", "Fixture\t"),
        ("user_agent", "\r\nFixture"),
        ("user_agent", "Fixture\x00"),
        ("user_agent", "Fixtür"),
        ("user_agent", "x" * 1025),
        ("user_agent", 1),
        ("referrer", "/entry"),
        ("referrer", "ftp://fixture.example/entry"),
        ("referrer", "https:///fixture.example/entry"),
        ("referrer", "https://fixture.example:65536/entry"),
        ("referrer", "https://user:pass@fixture.example/entry"),
        ("referrer", "https://@fixture.example/entry"),
        ("referrer", "https://fixture.example/entry#"),
        ("referrer", " https://fixture.example/entry"),
        ("referrer", "https://fixture.example/entry\n"),
        ("referrer", "https://fixture.example/entry\x85"),
        ("referrer", "https://fixture.example/entry\x9f"),
        ("referrer", r"https://fixture.example\entry"),
        ("referrer", "https://fixture.example/" + "x" * 2048),
    ],
)
def test_batch_rejects_invalid_browser_setting(field, value):
    with pytest.raises(ValidationError) as error:
        data(**{field: value})
    assert error.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("adapter", ["manual_chrome", "kameleo", "cloakbrowser"])
def test_referrer_is_available_when_custom_user_agent_is_incompatible(adapter):
    settings = {
        "adapters": ["playwright", adapter],
        "preparation": {"mode": "manual"} if adapter == "manual_chrome" else {},
        "referrer": "http://fixture.example/entry?from=qa",
    }
    assert data(**settings).referrer == settings["referrer"]
    with pytest.raises(ValidationError, match="Custom User-Agent requires Playwright Chromium"):
        data(**settings, user_agent="ABRL Fixture/1.0")


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


def test_session_failure_with_confirmed_shutdown_leaves_batch_siblings_running(db, monkeypatch):
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    batch = create(db, count=3, concurrency=2, mode="live")
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    failed = session_runner.claim("standard")
    assert failed is not None
    with db.begin() as connection:
        connection.get(Target, "target").last_started_at = None
    active = session_runner.claim("standard")
    assert active is not None
    session_runner.record_activity(active, {"kind": "workflow_started"})
    assert session_runner.claim("standard") is None

    session_runner.finish(
        failed, BrowserResult("failed", 1, "Browser startup failed", {"runtime_stopped": True}, False)
    )
    with db.begin() as connection:
        assert connection.get(BrowserSession, failed.id).status == "failed"
        assert connection.get(BrowserProfile, failed.profile_id).owner_session_id is None
        assert connection.get(BrowserSession, active.id).status == "running"
        assert connection.get(BrowserSession, active.id).lease_token == active.lease
        assert connection.get(SessionBatch, batch.id).status == "running"
        connection.get(Target, "target").last_started_at = None
        session_runner.admission_tick(connection)

    queued = session_runner.claim("standard")
    assert queued is not None and queued.id not in {failed.id, active.id}
    session_runner.finish(queued, BrowserResult("completed", 1, None, {"runtime_stopped": True}, True))
    with db() as connection:
        assert connection.get(SessionBatch, batch.id).finished_at is None

    session_runner.finish(active, BrowserResult("completed", 1, None, {"runtime_stopped": True}, True))
    with db() as connection:
        view = platform_service.batch_view(connection, connection.get(SessionBatch, batch.id))
        assert view["status"] == "completed" and view["finished_at"] is not None
        assert view["counts"] == {"completed": 2, "failed": 1}


def test_missing_snapshot_manifest_fails_only_one_queued_session(db, monkeypatch):
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    batch = create(db, count=2, concurrency=2, mode="live", persistence="persistent")
    with db.begin() as connection:
        broken, sibling = connection.scalars(
            select(BrowserSession)
            .where(BrowserSession.batch_id == batch.id)
            .order_by(BrowserSession.ordinal)
        ).all()
        connection.get(BrowserProfile, broken.profile_id).snapshot_key = "private-fixture-snapshot.bundle"
        session_runner.admission_tick(connection)
        assert broken.admitted and sibling.admitted

    work = session_runner.claim("standard")
    assert work is not None and work.id == sibling.id
    with db() as connection:
        failed = connection.get(BrowserSession, broken.id)
        assert failed.status == "failed" and failed.finished_at is not None
        assert failed.error == "Profile snapshot manifest is missing"
        assert not failed.admitted
        assert failed.attempt_number == 0 and failed.started_at is None
        assert failed.lease_token is None and failed.lease_until is None and failed.owner_worker is None
        assert connection.get(BrowserProfile, failed.profile_id).owner_session_id is None
        attempt = connection.scalar(select(SessionAttempt).where(SessionAttempt.session_id == failed.id))
        assert attempt is None
        assert connection.get(SessionBatch, batch.id).status == "running"

    session_runner.finish(work, BrowserResult("completed", 1, None, {"runtime_stopped": True}, True))
    with db() as connection:
        view = platform_service.batch_view(connection, connection.get(SessionBatch, batch.id))
        assert view["status"] == "completed"
        assert view["counts"] == {"completed": 1, "failed": 1}


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

        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": "https://schedule.example"}
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


@pytest.mark.parametrize("mode, origin", [
    ("demo", "https://schedule.example"),
    ("live", "http://127.0.0.1:8400"),
    ("live", "https://old-lab.example"),
])
def test_retired_schedule_pauses_until_live_revision_and_resume(db, monkeypatch, mode, origin):
    from abrl import scheduling

    monkeypatch.setattr(platform_service, "CONTROLLED_ORIGIN", "https://old-lab.example")
    monkeypatch.setattr(scheduling, "CONTROLLED_ORIGIN", "https://old-lab.example")
    instant = datetime.now(UTC)
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": origin}
        batch = data(mode=mode)
        config = ScheduleInput(
            name="Retired", kind="interval", interval_seconds=300, start_at=instant, batch=batch
        ).model_dump(mode="json")
        config["pinned"] = platform_service.batch_snapshot(connection, batch, "default")
        schedule = Schedule(project_id="default", name="Retired", config=config, next_fire_at=instant.isoformat())
        connection.add(schedule)
        connection.flush()
        schedule_id = schedule.id
        connection.add(ScheduledOccurrence(
            schedule_id=schedule_id, version=1, scheduled_for=instant.isoformat(), status="waiting"
        ))
        materialize_due(connection, instant + timedelta(seconds=1))
        assert not schedule.enabled and schedule.next_fire_at is None
        assert connection.scalar(select(ScheduledOccurrence)).status == "skipped"
        assert connection.scalar(select(SessionBatch)) is None
        target.config = {**target.config, "origin": "https://schedule.example"}
    base = f"/api/v1/schedules/{schedule_id}"
    with client(production=True) as api:
        listed = api.get("/api/v1/schedules").json()["items"][0]
        assert not listed["enabled"] and listed["pause_reason"]
        assert api.post(base + "/resume").status_code == 409
        assert api.post(base + "/run-now", headers={"Idempotency-Key": "retired-now"}).status_code == 409
        settings = data().model_dump(mode="json", exclude_unset=True)
        updated = api.put(base, json={"name": "Ready", "kind": "interval", "interval_seconds": 300, "batch": settings})
        assert updated.status_code == 200, updated.text
        assert not updated.json()["enabled"] and updated.json()["pause_reason"] is None
        assert api.post(base + "/resume").json()["enabled"] is True
        batch = api.post(base + "/run-now", headers={"Idempotency-Key": "retired-now"}).json()
        assert batch["config"]["artifact_policy"] == "all" and batch["config"]["capture_javascript"] is True


@pytest.mark.asyncio
async def test_observations_verified_negative_and_failed_positive_callback(db, monkeypatch):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "mode": "instrumented"}
    batch = create(db, mode="live", count=2, concurrency=2)
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
        client_ip="::ffff:8.8.8.8",
        user_agent="Test browser",
    )
    connector_secret = "test-connector-" + "c" * 32
    monkeypatch.setenv("ABRL_OBSERVATION_CONNECTOR_TOKEN", connector_secret)
    with client() as api:
        assert api.post("/api/v1/integration-observations", json=value.model_dump()).status_code == 401
        response = api.post(
            "/api/v1/integration-observations",
            headers={"Authorization": "Bearer " + connector_secret},
            json=value.model_dump(),
        )
        assert response.status_code == 200, response.text
        first = response.json()
    assert first["accepted"] is False and first["raw_code"] == 9
    assert "observed_ip" not in first
    with db() as connection:
        assert connection.get(ProviderObservation, first["id"]).observed_ip == "8.8.8.8"
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
        update={
            "context": observations.context_token(other.id, other.lease, other.epoch),
            "client_ip": "8.8.8.8",
        }
    )
    result = await observations.ingest(value, source="site_connector")
    assert result["accepted"] is False and result["verification_status"] == "failed"
    with client() as api:
        assert api.get(f"/api/v1/batches/{batch.id}/ip-summary").json() == {
            "expected_sessions": 2,
            "observed_sessions": 2,
            "distinct_ips": 1,
            "status": "duplicate",
        }


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
