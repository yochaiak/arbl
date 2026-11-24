"""Registration grants launch readiness within the site's configured origins."""

import pytest
import test_platform
from sqlalchemy import select
from test_platform import client

from abrl import runtime, session_runner
from abrl.db import Trial

db = test_platform.db


@pytest.mark.parametrize("kind", ["run", "batch"])
def test_registered_site_runs_without_verification_and_stale_version_stops(db, monkeypatch, kind):
    monkeypatch.setattr(runtime, "Session", db)

    with client() as api:
        registration = {
            "name": "Owned storefront",
            "origin": "https://Store.example:443/",
            "navigation_origins": ["https://checkout.example", "https://CHECKOUT.example:443/"],
            "resource_origins": ["https://cdn.example"],
        }
        response = api.post("/api/v1/targets", json=registration)
        assert response.status_code == 201, response.text
        target = response.json()
        assert target["authorized"] and target["live_ready"]
        assert target["origin"] == "https://store.example"
        assert target["navigation_origins"] == ["https://checkout.example"]
        assert target["verified"] is False
        assert "proof_token" not in target and "proof_path" not in target
        assert api.post(f"/api/v1/targets/{target['id']}/verify").status_code == 404

        payload = {"target_id": target["id"], "workflow_id": "workflow"}
        if kind == "run":
            endpoint = "/api/v1/runs"
            payload["repetitions"] = 1
            response = api.post(endpoint, json=payload)
            assert response.status_code == 201, response.text
            with db() as connection:
                trial_id = connection.scalar(select(Trial.id).where(Trial.run_id == response.json()["id"]))
            claim = runtime.claim(trial_id)
            heartbeat = runtime.heartbeat
        else:
            endpoint = "/api/v1/batches"
            response = api.post(endpoint, json={**payload, "count": 1, "idempotency_key": "launch-original-site"})
            assert response.status_code == 201, response.text
            with db.begin() as connection:
                session_runner.admission_tick(connection)
            claim = session_runner.claim("standard")
            heartbeat = session_runner.heartbeat

        assert claim is not None
        assert heartbeat(claim) is False
        changed = api.put(
            f"/api/v1/targets/{target['id']}",
            json={**registration, "origin": "https://updated.example"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["version"] == target["version"] + 1
        assert changed.json()["live_ready"]
        assert heartbeat(claim) is True


@pytest.mark.parametrize(
    ("origin", "navigation_origins", "authorized"),
    [
        ("https://*.store.example", [], True),
        ("https://store.example/path", [], True),
        ("https://127.0.0.1", [], True),
        ("https://store.example", ["https://192.168.0.1"], True),
        ("https://store.example", ["http://127.0.0.1:8400"], True),
        ("https://store.example", [], False),
    ],
)
def test_registration_preserves_origin_and_authorization_validation(db, origin, navigation_origins, authorized):
    with client() as api:
        response = api.post(
            "/api/v1/targets",
            json={"name": "Site", "origin": origin, "navigation_origins": navigation_origins, "authorized": authorized},
        )
        assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("https://checkout.example/checkout", 201),
        ("https://sub.checkout.example/checkout", 409),
        ("https://assets.example/checkout", 409),
    ],
)
def test_registration_preserves_exact_navigation_scope(db, destination, expected):
    with client() as api:
        site = api.post(
            "/api/v1/targets",
            json={
                "name": "Test storefront",
                "origin": "https://store.example",
                "navigation_origins": ["https://checkout.example"],
                "resource_origins": ["https://assets.example"],
            },
        ).json()
        assert site["live_ready"]
        workflow = api.post(
            "/api/v1/workflows",
            json={"name": "Navigate", "steps": [{"action": "navigate", "url": destination}]},
        ).json()
        response = api.post(
            "/api/v1/batches",
            json={
                "target_id": site["id"],
                "workflow_id": workflow["id"],
                "count": 1,
                "idempotency_key": "test-navigation-scope",
            },
        )
        assert response.status_code == expected, response.text
        if expected == 409:
            assert "registered target scope" in response.json()["detail"]
