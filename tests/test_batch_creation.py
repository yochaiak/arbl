"""Allocation failures retain independent successes and never replay launched work."""

import test_platform
from sqlalchemy import select
from test_platform import client, data

from abrl.platform_models import BrowserProfile, BrowserSession, SessionBatch, SessionBatchItem
from abrl.platform_service import settle_batch

db = test_platform.db


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
