"""Allocation failures retain independent successes and never replay launched work."""

import test_platform
from sqlalchemy import select
from test_platform import client, data

from abrl.platform_models import BrowserProfile, BrowserSession, SessionBatch, SessionBatchItem
from abrl.platform_service import digest, settle_batch

db = test_platform.db


def test_live_defaults_reuse_server_seed_and_reject_stale_workflow(db, monkeypatch):
    from abrl import platform_service

    seeds = iter((31, 79))
    monkeypatch.setattr(platform_service.secrets, "randbelow", lambda _limit: next(seeds))
    payload = {"target_id": "target", "workflow_id": "workflow", "count": 1, "idempotency_key": "live-defaults"}
    with client() as api:
        url = "/api/v1/batches"
        changed = api.post(url, json={**payload, "expected_workflow_version": 2})
        assert changed.status_code == 409 and "workflow changed" in changed.json()["detail"]
        first = api.post(url, json={**payload, "expected_workflow_version": 1})
        assert first.status_code == 201, first.text
        config = first.json()["config"]
        assert (config["mode"], config["seed"], config["artifact_policy"], config["capture_javascript"]) == (
            "live", 31, "all", True,
        )
        assert api.post(url, json={**payload, "expected_workflow_version": 1}).json() == first.json()
        next_batch = api.post(url, json={**payload, "idempotency_key": "live-defaults-next"})
        assert next_batch.json()["config"]["seed"] == 79


def test_mixed_batch_reuses_compatible_profiles_in_requested_order(db):
    with db.begin() as connection:
        connection.add_all([
            BrowserProfile(id="human", project_id="default", name="Human", adapter="manual_chrome", persistence="fresh"),
            BrowserProfile(id="auto", project_id="default", name="Auto", adapter="playwright", persistence="fresh"),
        ])
    payload = {
        "target_id": "target", "workflow_id": "workflow", "count": 2,
        "adapters": ["playwright", "manual_chrome"], "profile_ids": ["human", "auto"],
        "seed": 0, "idempotency_key": "mixed-profile-order",
    }
    with client() as api:
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
        batch = response.json()
        assert batch["creation"] == {"created": 2}
        assert batch["config"]["preparation"]["mode"] == "none"
        items = api.get(f"/api/v1/batches/{batch['id']}/items").json()["items"]
        assert [item["adapter"] for item in items] == ["manual_chrome", "playwright"]


def profiles(db):
    with db.begin() as connection:
        connection.add_all(
            [
                BrowserProfile(
                    id="available-profile",
                    project_id="default",
                    name="Available",
                    adapter="playwright",
                    persistence="persistent",
                ),
                BrowserProfile(
                    id="quarantined-profile",
                    project_id="default",
                    name="Quarantined",
                    adapter="playwright",
                    persistence="persistent",
                    status="quarantined",
                ),
            ]
        )


def test_partial_creation_retry_is_idempotent_and_never_recreates_success(db):
    profiles(db)
    with client() as api:
        payload = data(count=2, profile_ids=["available-profile", "quarantined-profile"]).model_dump(
            mode="json"
        )
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
        batch = response.json()
        assert batch["creation"] == {"created": 1, "failed": 1}
        assert api.post("/api/v1/batches", json=payload).json()["id"] == batch["id"]
        items = api.get(f"/api/v1/batches/{batch['id']}/items").json()["items"]
        first_id = items[0]["session_id"]
        assert items[1]["error"] and items[1]["session_id"] is None
        url = f"/api/v1/batches/{batch['id']}/retry-creation"
        key = {"Idempotency-Key": "allocation-retry-1"}
        assert api.post(url, headers=key).json()["creation"]["failed"] == 1
        with db.begin() as connection:
            connection.get(BrowserProfile, "quarantined-profile").status = "available"
        # Replaying an acknowledged failed request cannot silently cause a new effect.
        assert api.post(url, headers=key).json()["creation"]["failed"] == 1
        response = api.post(url, headers={"Idempotency-Key": "allocation-retry-2"})
        assert response.status_code == 200, response.text
        assert response.json()["creation"] == {"created": 2}
        items = api.get(f"/api/v1/batches/{batch['id']}/items").json()["items"]
        assert items[0]["session_id"] == first_id and items[0]["attempts"] == 1
        assert items[1]["attempts"] == 3 and items[1]["session_id"] != first_id
    with db() as connection:
        assert len(list(connection.scalars(select(BrowserSession)))) == 2


def test_batch_retries_use_preexisting_hash_when_browser_settings_are_unset(db):
    historical = data(mode="demo", seed=42, artifact_policy="none", name="Browser experiment").model_dump(
        mode="json",
        exclude={
            "user_agent", "referrer", "capture_javascript", "fixture_params", "device_preset", "batch_presets",
            "expected_workflow_version", "expected_dependencies", "session_groups", "workflow_inputs",
        },
    )
    with db.begin() as connection:
        row = SessionBatch(
            project_id="default",
            name=historical["name"],
            config=historical,
            request_hash=digest(historical),
            idempotency_key=historical["idempotency_key"],
            count=historical["count"],
            concurrency=historical["concurrency"],
        )
        connection.add(row)
    with client() as api:
        for request in (
            historical,
            {**historical, "user_agent": None, "referrer": None},
            {**historical, "capture_javascript": False},
        ):
            response = api.post("/api/v1/batches", json=request)
            assert response.status_code == 201, response.text
            assert response.json()["id"] == row.id
        for setting in ("user_agent", "referrer"):
            response = api.post(
                "/api/v1/batches", json={**historical, setting: "https://fixture.example/source"}
            )
            assert response.status_code == 409, response.text


def test_javascript_capture_is_live_only_and_part_of_launch_identity(db):
    with client() as api:
        url = "/api/v1/batches"
        demo = data(mode="demo").model_dump(mode="json")
        rejected = api.post(url, json={**demo, "capture_javascript": True})
        assert rejected.status_code == 422 and "require live execution" in rejected.text

        live = {**demo, "mode": "live", "capture_javascript": False}
        disabled = api.post(url, json=live)
        assert disabled.status_code == 201, disabled.text
        assert disabled.json()["config"]["capture_javascript"] is False
        assert api.post(url, json={**live, "capture_javascript": True}).status_code == 409

        enabled_payload = {**live, "idempotency_key": "javascript-capture", "capture_javascript": True}
        enabled = api.post(url, json=enabled_payload)
        assert enabled.status_code == 201, enabled.text
        assert enabled.json()["config"]["capture_javascript"] is True
        assert api.post(url, json=enabled_payload).json()["id"] == enabled.json()["id"]
        assert api.post(url, json={**enabled_payload, "capture_javascript": False}).status_code == 409


def test_creation_errors_prevent_false_completion_and_cancel_prevents_retry(db):
    with client() as api:
        response = api.post(
            "/api/v1/batches", json=data(profile_ids=["unavailable"]).model_dump(mode="json")
        )
        batch = response.json()
        assert batch["status"] == "creation_failed" and batch["finished_at"] is None
        assert api.post(f"/api/v1/batches/{batch['id']}/cancel").json()["status"] == "cancelled"
        assert (
            api.post(
                f"/api/v1/batches/{batch['id']}/retry-creation",
                headers={"Idempotency-Key": "allocation-retry"},
            ).status_code
            == 409
        )
    with db() as connection:
        item = connection.scalar(select(SessionBatchItem))
        assert item.status == "cancelled" and item.session_id is None


def test_profile_queued_in_other_batch_is_a_per_item_creation_failure(db):
    profiles(db)
    with client() as api:
        first = data(profile_ids=["available-profile"]).model_dump(mode="json")
        batch = api.post("/api/v1/batches", json=first).json()
        second = api.post("/api/v1/batches", json={**first, "idempotency_key": "second-request"})
        assert second.status_code == 201 and second.json()["creation"] == {"failed": 1}
        with db.begin() as connection:
            session = connection.scalar(
                select(BrowserSession).where(BrowserSession.batch_id == batch["id"])
            )
            session.status = "completed"
            settle_batch(connection, batch["id"])
            assert connection.get(SessionBatch, batch["id"]).status == "completed"


def test_historical_proxy_configuration_stays_readable_after_capability_validation(db):
    from abrl.platform_models import ProxyInventory

    with db.begin() as connection:
        connection.add(
            ProxyInventory(
                project_id="default",
                name="Historical gateway",
                config={
                    "name": "Historical gateway",
                    "product": "isp",
                    "host": "pr.oxylabs.io",
                    "port": 7777,
                    "username_ref": "USER",
                    "password_ref": "PASSWORD",
                    "country": "IL",
                    "capacity": 1,
                    "max_duration_minutes": 60,
                    "exclusive": False,
                    "cost_per_gb": None,
                },
            )
        )
    with client() as api:
        response = api.get("/api/v1/proxy-inventory")
        assert response.status_code == 200, response.text
        row = response.json()["items"][0]
        assert row["product"] == "isp" and row["host"] == "pr.oxylabs.io"
        assert row["readiness"]["status"] == "unsupported_configuration"
        assert (
            api.post(
                "/api/v1/proxy-inventory",
                json={
                    key: value
                    for key, value in row.items()
                    if key not in {"id", "active_leases", "health", "readiness"}
                },
            ).status_code
            == 422
        )
