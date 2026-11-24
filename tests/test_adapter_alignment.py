import pytest
from sqlalchemy import select
from test_platform import client, data
from test_platform import db as db

from abrl import platform_api, platform_service, session_runner
from abrl.db import WorkflowVersion, now
from abrl.platform_models import BrowserProfile, BrowserSession, FleetSlot


@pytest.fixture
def adapter_inventory(monkeypatch):
    from abrl.adapters import adapter_capabilities

    inventory = adapter_capabilities()
    for entry in inventory:
        entry.update(available=True, status="configured", reason="Fixture adapter")
    monkeypatch.setattr(platform_api, "execution_capabilities", lambda: inventory)
    monkeypatch.setattr(platform_service, "execution_capabilities", lambda: inventory)
    return inventory


def test_adapter_view_separates_capabilities_configuration_and_live_capacity(db, adapter_inventory):
    with db.begin() as connection:
        connection.add_all([
            FleetSlot(id="standard", pool="standard", capacity=3, seen_at=now(),
                      metadata_json={"adapters": ["playwright", "kameleo"]}),
            FleetSlot(id="legacy", pool="vendor", capacity=1, seen_at=now()),
        ])
    with client() as api:
        response = api.get("/api/v1/adapters").raise_for_status()
    adapters = {entry["id"]: entry for entry in response.json()}
    assert adapters["playwright"]["worker_capacity"] == 3
    assert adapters["manual_chrome"]["worker_capacity"] == 0
    assert adapters["kameleo"]["worker_capacity"] is None
    assert adapters["manual_chrome"]["features"]["workflow_control"] == "operator"
    assert adapters["kameleo"]["features"]["persistent_profiles"] == "requires_entitlement"
    assert adapters["cloakbrowser"]["features"]["batch_preset_application"] == "viewport_only"
    assert {entry["verification"] for entry in adapters.values()} == {"unverified"}


@pytest.mark.parametrize("grouped", [False, True])
def test_reused_profile_persistence_and_snapshot_match_in_both_batch_shapes(db, grouped):
    with db.begin() as connection:
        connection.add(BrowserProfile(
            id="reused", name="Saved fixture", adapter="playwright", project_id="default", persistence="persistent",
        ))
    args = {"session_groups": [{"adapter": "playwright", "profile_ids": ["reused"]}]} if grouped else {
        "profile_ids": ["reused"],
    }
    with db.begin() as connection:
        batch = platform_service.create_batch(connection, data(persistence="fresh", **args), "default")
        row = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == batch.id))
        assert row.persistence == "persistent"


def test_fresh_claim_ignores_a_historical_snapshot_key(db, adapter_inventory):
    with db.begin() as connection:
        batch = platform_service.create_batch(connection, data(), "default")
        row = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == batch.id))
        connection.get(BrowserProfile, row.profile_id).snapshot_key = "obsolete-snapshot"
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard", supported_adapters=["playwright"])
    assert work is not None and work.persistence == "fresh" and work.snapshot is None


def test_mixed_batch_keeps_eligible_sessions_moving_and_preflight_explains_operator_block(db, adapter_inventory):
    with db.begin() as connection:
        version = connection.get(WorkflowVersion, ("workflow", 1))
        version.config = {**version.config, "steps": [{"action": "navigate", "url": "/lab"}]}
    payload = data(session_groups=[{"adapter": "manual_chrome"}, {"adapter": "playwright"}]).model_dump(mode="json")
    with client() as api:
        preflight = api.post("/api/v1/batches/preflight", json=payload).raise_for_status().json()
        assert {notice["code"] for notice in preflight["notices"] if notice["group"] == 1} >= {
            "operator_success_check_required", "operator_reserved_steps",
        }
        batch = api.post("/api/v1/batches", json=payload).raise_for_status().json()
    with db.begin() as connection:
        session_runner.admission_tick(connection)
        rows = {row.adapter: row for row in connection.scalars(
            select(BrowserSession).where(BrowserSession.batch_id == batch["id"]),
        )}
        assert rows["manual_chrome"].status == "blocked"
        assert rows["playwright"].admitted


def test_diagnostic_preflight_uses_effective_profile_and_adapter_requirements(db, adapter_inventory, monkeypatch):
    monkeypatch.setattr(platform_service, "is_managed_diagnostic", lambda *_: True)
    payload = data(session_groups=[
        {"adapter": "playwright"},
        {"adapter": "manual_chrome"},
        {"adapter": "kameleo", "persistence": "persistent"},
    ]).model_dump(mode="json")
    with client() as api:
        response = api.post("/api/v1/batches/preflight", json=payload).raise_for_status().json()
        manual = api.post("/api/v1/batches/preflight", json=data(adapters=["manual_chrome"]).model_dump(mode="json"))
        assert manual.status_code == 409
        assert "fresh automated session" in manual.json()["detail"]
    assert {item["group"] for item in response["notices"] if item["code"] == "diagnostic_configuration"} == {2, 3}
    assert [item["group"] for item in response["notices"] if item["code"] == "persistent_profile_entitlement"] == [3]
