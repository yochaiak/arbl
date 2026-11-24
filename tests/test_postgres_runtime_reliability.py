"""Exercise local PostgreSQL row locks with isolated, disposable schemas."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema
from test_platform import db as db

from abrl import access, platform_api, platform_service, session_runner
from abrl.api import app
from abrl.browser import BrowserResult
from abrl.config import DATABASE_URL
from abrl.db import Base, Target, TargetVersion, Workflow, WorkflowVersion
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Schedule,
    ScheduledOccurrence,
    SessionAttempt,
    SessionBatch,
    SessionBatchItem,
)
from abrl.platform_schemas import BatchInput, ScheduleInput
from abrl.scheduling import materialize_due

pytestmark = pytest.mark.integration
OPERATOR_TOKEN = "fixture-token-without-live-credentials"


@pytest.fixture
def postgres_runtime(monkeypatch):
    url = make_url(DATABASE_URL)
    if url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("Run the local Docker PostgreSQL stack for real row-lock tests")
    schema = f"abrl_runtime_test_{uuid4().hex}"
    admin = create_engine(url)
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        for module in (access, platform_api, session_runner):
            monkeypatch.setattr(module, "Session", factory)
        monkeypatch.setattr(access, "OPERATOR_TOKEN", OPERATOR_TOKEN)
        monkeypatch.setattr(
            platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
        )
        with factory.begin() as db:
            access.ensure_bootstrap(db)
            db.add_all([
                Target(id="local-target", version=1, verified_version=1, proof_token="fixture"),
                Workflow(id="local-workflow", version=1),
            ])
            db.flush()
            db.add_all([
                TargetVersion(target_id="local-target", version=1, config={
                    "name": "Authorized fixture", "origin": "https://fixture.example",
                    "mode": "browser_only", "resource_origins": [], "concurrency": 5, "rate_limit": 60,
                }),
                WorkflowVersion(workflow_id="local-workflow", version=1, config={
                    "name": "Fixture workflow", "steps": [{"action": "navigate", "url": "/"}],
                }),
            ])
        yield factory
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


def settings():
    return BatchInput(
        target_id="local-target", workflow_id="local-workflow", idempotency_key="fixture-001", count=1,
    )


def test_stop_commits_before_waiting_finish_can_retry(postgres_runtime):
    factory = postgres_runtime
    with factory.begin() as db:
        platform_service.create_batch(db, settings(), "default")
        session_runner.admission_tick(db)
    work = session_runner.claim("standard")
    assert work
    entered = Event()

    def finish():
        entered.set()
        session_runner.finish(work, BrowserResult(
            "failed", 1, "Transient browser startup failure",
            {"runtime_stopped": True, "failure_kind": "startup_transient"}, False,
        ))

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as db:
            row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
            row.status = "stopping"
            future = pool.submit(finish)
            assert entered.wait(timeout=3)
        future.result(timeout=10)

    with factory() as db:
        row = db.get(BrowserSession, work.id)
        assert row.status == "cancelled" and row.next_attempt_at is None
        assert db.get(SessionAttempt, work.lease).outcome == "cancelled"


def test_concurrent_schedule_materializers_create_one_batch(postgres_runtime):
    factory = postgres_runtime
    scheduled = datetime.now(UTC)
    with factory.begin() as db:
        definition = ScheduleInput(
            name="Fixture schedule", kind="once", start_at=scheduled, batch=settings()
        ).model_dump(mode="json")
        definition["pinned"] = platform_service.batch_snapshot(db, settings(), "default")
        db.add(Schedule(
            id="local-schedule", project_id="default", name="Fixture schedule",
            config=definition, next_fire_at=scheduled.isoformat(),
        ))
    start = Barrier(2)

    def materialize():
        start.wait(timeout=3)
        with factory.begin() as db:
            materialize_due(db, scheduled + timedelta(seconds=1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(materialize) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)

    with factory() as db:
        occurrence = db.scalars(select(ScheduledOccurrence)).one()
        assert occurrence.status == "dispatched"
        assert db.scalars(select(SessionBatch)).one().id == occurrence.batch_id


@pytest.mark.parametrize("replay", [False, True], ids=["competing-batches", "duplicate-request"])
def test_concurrent_immediate_batches_reserve_existing_profile_once(postgres_runtime, replay):
    factory = postgres_runtime
    profile_id = "existing-profile"
    with factory.begin() as db:
        db.add(BrowserProfile(
            id=profile_id, project_id="default", name="Existing profile",
            adapter="playwright", persistence="persistent",
        ))
    start = Barrier(2)
    payload = {
        "target_id": "local-target",
        "workflow_id": "local-workflow",
        "session_groups": [{"adapter": "playwright", "profile_ids": [profile_id]}],
    }

    def launch(key):
        with TestClient(app, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}) as client:
            start.wait(timeout=5)
            response = client.post("/api/v1/batches", json={**payload, "idempotency_key": key})
        assert response.status_code == 201, response.text
        return response.json()

    keys = ("group-launch-1", "group-launch-1" if replay else "group-launch-2")
    with ThreadPoolExecutor(max_workers=2) as pool:
        launches = [pool.submit(launch, key) for key in keys]
        results = [item.result(timeout=12) for item in launches]

    with factory() as db:
        session = db.scalars(select(BrowserSession)).one()
        assert session.profile_id == profile_id and session.persistence == "persistent"
        assert len(list(db.scalars(select(SessionBatch)))) == (1 if replay else 2)
        items = list(db.scalars(select(SessionBatchItem)))
        assert len(items) == (1 if replay else 2)
        winning_item = next(item for item in items if item.session_id == session.id)
        assert winning_item.status == "created"
        if not replay:
            failed_item = next(item for item in items if item.status == "failed")
            assert failed_item.session_id is None and "reserved" in failed_item.error
            assert {result["status"] for result in results} == {"queued", "creation_failed"}
            return

    assert results[0] == results[1]
    with TestClient(app, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}) as client:
        submitted = {**payload, "idempotency_key": keys[0]}
        response = client.post("/api/v1/batches", json=submitted)
        assert response.status_code == 201 and response.json() == results[0]
        conflict = client.post("/api/v1/batches", json={**submitted, "name": "Different request"})
        assert conflict.status_code == 409


@pytest.mark.parametrize(("database", "workflow_id"), [("postgres_runtime", "local-workflow"), ("db", "workflow")])
def test_concurrent_claims_share_decodo_account_across_inventory(database, workflow_id, request, monkeypatch):
    from abrl.platform_models import ProxyInventory, ProxyLease, ProxyPolicy
    from abrl.platform_schemas import ProxyInventoryInput, ProxyPolicyInput

    factory = request.getfixturevalue(database)
    monkeypatch.setattr(platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}])
    monkeypatch.setenv(
        "ABRL_PROXY_ACCOUNT_GROUPS",
        '{"shared":{"provider":"decodo","username_refs":["ACCOUNT_A","ACCOUNT_B"],"max_sessions":1}}',
    )
    with factory.begin() as db:
        for suffix in ("a", "b"):
            origin = f"https://{suffix}.example"
            target_id = f"target-{suffix}"
            policy = ProxyPolicyInput(name=suffix, provider="decodo", target_origin=origin, provider_resolved_ack=True)
            inventory = ProxyInventoryInput(name=suffix, provider="decodo", target_origin=origin,
                                            username_ref=f"ACCOUNT_{suffix.upper()}", password_ref="PASSWORD", capacity=5)
            db.add_all([
                Target(id=target_id, version=1, verified_version=1, proof_token="fixture"),
                ProxyInventory(project_id="default", name=suffix, config=inventory.model_dump()),
                ProxyPolicy(id=suffix, project_id="default", name=suffix, config=policy.model_dump()),
            ])
            db.flush()
            db.add(TargetVersion(target_id=target_id, version=1, config={
                "name": suffix, "origin": origin, "resource_origins": [], "mode": "browser_only", "authorized": True,
                "rate_limit": 60, "concurrency": 5,
            }))
            db.flush()
            platform_service.create_batch(db, settings().model_copy(update={
                "target_id": target_id, "workflow_id": workflow_id, "idempotency_key": f"shared-account-{suffix}",
                "proxy_policy_id": suffix, "mode": "live",
            }), "default")
        session_runner.admission_tick(db)
        sessions = db.scalars(select(BrowserSession)).all()
        assert sum(row.admitted for row in sessions) == 1
        # Simulate stale admissions at two workers. Claim must still enforce the cap.
        for row in sessions:
            row.admitted = True
    start = Barrier(2)

    def claim():
        start.wait(timeout=5)
        return session_runner.claim("standard")

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(claim) for _ in range(2)]
        works = [work for job in jobs if (work := job.result(timeout=15)) is not None]
    assert len(works) == 1
    with factory() as db:
        assert len(db.scalars(select(ProxyLease).where(ProxyLease.status == "active")).all()) == 1
    session_runner.finish(works[0], BrowserResult("blocked", 1, "Fixture", {"runtime_stopped": True}, False))
    with factory.begin() as db:
        session_runner.admission_tick(db)
    assert session_runner.claim("standard") is not None
