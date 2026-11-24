from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_platform import client, data
from test_platform import db as db
from test_proxy_platform import proxy_runtime as proxy_runtime

from abrl import session_runner
from abrl.db import TargetVersion, WorkflowVersion
from abrl.diagnostic_fixture import DIAGNOSTIC_PATH
from abrl.platform_models import BrowserSession, ProxyLease, SessionAttempt

ORIGIN = "https://diagnostics.owned-fixture.dev"


def configure_target(db, path):
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": ORIGIN}
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {**workflow.config, "steps": [{"action": "navigate", "url": path}]}


@pytest.mark.parametrize(("path", "policy_assigned", "notice"), [
    (DIAGNOSTIC_PATH, True, "proxy_capacity"),
    ("/reports", True, "proxy_direct_fallback"),
    (DIAGNOSTIC_PATH, False, None),
])
def test_owned_proxy_diagnostic_requires_capacity_in_preview_admission_and_claim(
    db, proxy_runtime, monkeypatch, path, policy_assigned, notice,
):
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", ORIGIN)
    configure_target(db, path)
    with client(production=True) as api:
        policy = api.post("/api/v1/proxy-policies", json={
            "name": "Optional direct fallback", "allow_direct": True, "continuity": "none",
        })
        assert policy.status_code == 201, policy.text
        payload = data(proxy_policy_id=policy.json()["id"] if policy_assigned else None).model_dump(mode="json")
        preview = api.post("/api/v1/batches/preflight", json=payload)
        assert preview.status_code == 200, preview.text
        codes = {entry["code"] for entry in preview.json()["notices"]} & {"proxy_capacity", "proxy_direct_fallback"}
        assert codes == ({notice} if notice else set())
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
    if notice == "proxy_capacity":
        monkeypatch.delenv("ABRL_DIAGNOSTIC_URL")  # A worker with config drift still honors the pinned route.
    with db.begin() as connection:
        session_runner.admission_tick(connection)
        row = connection.scalars(select(BrowserSession)).one()
        assert row.admitted == (notice != "proxy_capacity")
        if notice == "proxy_capacity":
            assert row.queue_reason == "proxy_capacity"
            row.admitted = True  # Claim must independently check a stale admission.
    work = session_runner.claim("standard")
    if notice == "proxy_capacity":
        assert work is None
        with db() as connection:
            assert connection.scalar(select(SessionAttempt.id)) is None
            assert connection.scalar(select(ProxyLease.id)) is None
    else:
        assert work is not None and work.proxy is None


def test_malformed_fixture_configuration_is_rejected_only_for_diagnostic_path(db, proxy_runtime, monkeypatch):
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", ORIGIN + "/private-fixture-value")
    configure_target(db, DIAGNOSTIC_PATH)
    payload = data().model_dump(mode="json")
    with client(production=True) as api:
        for endpoint in ("/api/v1/batches/preflight", "/api/v1/batches"):
            response = api.post(endpoint, json=payload)
            assert response.status_code == 409
            assert "private-fixture-value" not in response.text
        configure_target(db, "/reports")
        assert api.post("/api/v1/batches/preflight", json=payload).status_code == 200


@pytest.mark.parametrize(("change", "reason"), [
    ("missing_binding", "diagnostic_proxy_binding_required"),
    ("provider_mismatch", "diagnostic_proxy_binding_required"),
    ("missing_lease", "diagnostic_proxy_binding_required"),
    ("removed_origin", "fixture_configuration"),
    ("malformed_origin", "fixture_configuration"),
    ("changed_origin", "fixture_configuration"),
])
async def test_pinned_diagnostic_stops_before_browser_if_assignment_changes(
    db, proxy_runtime, monkeypatch, tmp_path, change, reason,
):
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", ORIGIN)
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "work")
    browser_run = AsyncMock()
    monkeypatch.setattr(session_runner, "execute_workflow", browser_run)
    configure_target(db, DIAGNOSTIC_PATH)
    with client(production=True) as api:
        response = api.post("/api/v1/batches", json=data().model_dump(mode="json"))
        assert response.status_code == 201, response.text
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None and work.config["owned_diagnostic"] and work.proxy is None
    if change in {"missing_binding", "provider_mismatch", "missing_lease"}:
        work.config["proxy_policy"] = {"provider": "oxylabs", "allow_direct": True}
        if change == "provider_mismatch":
            work.proxy = {"provider": "decodo"}
        elif change == "missing_lease":
            work.proxy = {"provider": "oxylabs"}
    elif change == "removed_origin":
        monkeypatch.delenv("ABRL_DIAGNOSTIC_URL")
    else:
        monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", "https://" if change == "malformed_origin" else "https://other.svc.dev")
    await session_runner.execute(work)
    browser_run.assert_not_awaited()
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == "blocked"
        assert row.metadata_json["browser_diagnostic"]["reason"] == reason
        assert row.next_attempt_at is None
