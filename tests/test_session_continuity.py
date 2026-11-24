import os

import pytest
from sqlalchemy import select
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_platform import client, create, data
from test_platform import db as db
from test_proxy_platform import proxy_runtime as proxy_runtime
from test_proxy_platform import register_and_claim, register_inventory

from abrl import platform_service, session_runner
from abrl.browser import BrowserResult, StepFailed
from abrl.db import Target, TargetVersion, Workflow, WorkflowVersion
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    ProxyInventory,
    ProxyLease,
    SessionAttempt,
    SessionEvent,
)
from abrl.profile_storage import ProfileStore


@pytest.mark.parametrize(("change", "issue"), [
    ("expire", "proxy_lease_expired"), ("release", "proxy_lease_unavailable"),
    ("old_epoch", "proxy_lease_unavailable"),
])
def test_proxy_lease_checked_again_before_action_dispatch(db, proxy_runtime, change, issue):
    work, _ = register_and_claim(db)
    assert session_runner.heartbeat(work) is False
    with db.begin() as connection:
        lease = connection.scalars(select(ProxyLease)).one()
        if change == "expire":
            lease.expires_at = platform_service.utc_after(-1)
        elif change == "release":
            session_runner.release_proxy(connection, connection.get(BrowserSession, work.id))
        else:
            lease.epoch -= 1

    with pytest.raises(StepFailed, match=issue):
        session_runner.record_activity(work, {"kind": "action_started", "action": "click", "non_idempotent": True})
    assert session_runner.poll_control(work) == {"command": "stop"}
    assert session_runner.heartbeat(work) is True
    with db() as connection:
        assert not connection.get(SessionAttempt, work.lease).started_action
        assert "non_idempotent_action_attempt" not in connection.get(BrowserSession, work.id).metadata_json
        assert connection.scalar(select(SessionEvent.detail).where(SessionEvent.kind == issue)) == {"attempt": 1}


@pytest.mark.parametrize("provider", ["oxylabs", "decodo"])
async def test_expired_assignment_stops_before_proxy_probe_or_browser_launch(db, proxy_runtime, monkeypatch, tmp_path, provider):
    work, inventory_id = register_and_claim(
        db, provider=provider, continuity="sticky" if provider == "decodo" else "strict"
    )
    with db.begin() as connection:
        lease = connection.scalar(select(ProxyLease))
        lease.expires_at = platform_service.utc_after(-1)

    def unexpected_egress(*args, **kwargs):
        pytest.fail("Expired proxy assignment was used")

    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(session_runner, "run_decodo_preflight", unexpected_egress)
    monkeypatch.setattr(session_runner, "execute_workflow", unexpected_egress)
    await session_runner.execute(work)

    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        attempt = connection.get(SessionAttempt, work.lease)
        assert row.status == "cancelled" and row.workflow_outcome == "not_started"
        assert not attempt.started_action and row.metadata_json["runtime_stopped"]
        assert connection.get(ProxyLease, attempt.proxy_lease_id).status == "released"
        assert connection.get(ProxyInventory, inventory_id).active_leases == 0
        assert connection.execute(select(SessionEvent.detail).where(
            SessionEvent.session_id == work.id, SessionEvent.kind == "proxy_lease_expired",
        )).scalar_one() == {"attempt": 1}


def test_direct_retry_does_not_inherit_previous_proxy_assignment(db, proxy_runtime):
    with client() as api:
        inventory = register_inventory(api, None)
        policy = api.post("/api/v1/proxy-policies", json={
            "name": "Optional proxy", "continuity": "none", "allow_direct": True,
        })
        assert policy.status_code == 201, policy.text
    create(db, mode="live", proxy_policy_id=policy.json()["id"])
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    first = session_runner.claim("standard")
    assert first is not None and first.proxy is not None
    session_runner.finish(first, BrowserResult("failed", 1, "Browser startup failed", {
        "runtime_stopped": True, "failure_kind": "startup_transient",
    }, False))

    with db.begin() as connection:
        connection.get(ProxyInventory, inventory["id"]).enabled = False
        connection.get(Target, "target").last_started_at = None
        row = connection.get(BrowserSession, first.id)
        assert row.status == "queued"
        row.next_attempt_at = platform_service.utc_after(-1)
        session_runner.admission_tick(connection)
    second = session_runner.claim("standard")
    assert second is not None and second.id == first.id and second.proxy is None
    with db() as connection:
        old_lease_id = connection.get(SessionAttempt, first.lease).proxy_lease_id
        assert connection.get(ProxyLease, old_lease_id).status == "released"
        assert connection.get(BrowserSession, second.id).proxy_lease_id is None
        assert connection.get(SessionAttempt, second.lease).proxy_lease_id is None


@pytest.mark.integration
async def test_returning_chromium_batch_restores_cookie_and_storage_from_encrypted_snapshot(
    db, fixture, chromium, monkeypatch, tmp_path,
):
    origin, visited, _ = fixture
    store = ProfileStore(tmp_path / "snapshots", os.urandom(32))
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(session_runner, "snapshot_store", lambda: store)
    monkeypatch.setattr(session_runner, "profile_s3", lambda: None)
    monkeypatch.setattr(platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}])
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": origin}
        first = connection.get(WorkflowVersion, ("workflow", 1))
        first.config = {**first.config, "steps": [
            {"action": "navigate", "url": "/lab?profile=continuity-marker"},
            {"action": "wait_for_text", "selector": "#state", "value": "continuity-marker", "timeout_ms": 3000},
        ]}

    async def run_batch(request_key, profile_ids):
        request = data(persistence="persistent", profile_ids=profile_ids).model_copy(
            update={"idempotency_key": request_key}
        )
        with db.begin() as connection:
            platform_service.create_batch(connection, request, "default")
            session_runner.admission_tick(connection)
        work = session_runner.claim("standard")
        assert work is not None
        await session_runner.execute(work)
        with db() as connection:
            row = connection.get(BrowserSession, work.id)
            assert row.status == "completed", row.error
            assert row.metadata_json["runtime_stopped"] and row.metadata_json["profile_saved"]
            assert not (session_runner.WORK_ROOT / work.id / work.lease).exists()
            return work, row.metadata_json["profile_launch_state"]

    first_work, state = await run_batch("first-profile-run", [])
    assert state == "initialized" and first_work.snapshot is None
    with db.begin() as connection:
        connection.get(Target, "target").last_started_at = None
        connection.get(Workflow, "workflow").version = 2
        connection.add(WorkflowVersion(workflow_id="workflow", version=2, config={
            "name": "Read returning state", "steps": [
                {"action": "navigate", "url": "/lab?profile=read"},
                {"action": "assert_text", "selector": "#state", "value": "continuity-marker"},
                {"action": "assert_text", "selector": "#profile-cookie", "value": "continuity-marker"},
                {"action": "navigate", "url": "/lab?delayed-redirect"},
                {"action": "wait_for_text", "selector": "#redirect-state", "value": "Redirect complete", "timeout_ms": 3000},
            ],
        }))
    second_work, state = await run_batch("returning-profile-run", [first_work.profile_id])
    assert state == "restored" and second_work.snapshot is not None
    assert [path for _, path, _ in visited][-2:] == ["/lab?delayed-redirect", "/lab/form?redirected"]
    with db() as connection:
        profile = connection.get(BrowserProfile, first_work.profile_id)
        assert profile.owner_session_id is None and profile.version == 2
