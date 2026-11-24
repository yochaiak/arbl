"""Batch admission, adapter assignment, and retry coverage for QA presets."""

import pytest
import test_platform
from sqlalchemy import select
from test_platform import client

from abrl import platform_service, session_runner
from abrl.browser import BrowserResult
from abrl.db import Target
from abrl.platform_models import BrowserProfile, BrowserSession, SessionBatchItem

db = test_platform.db


def launch_payload(**overrides):
    return {
        "target_id": "target",
        "workflow_id": "workflow",
        "idempotency_key": "batch-presets-test",
        "count": 1,
        **overrides,
    }


def session_assignments(api, batch_id):
    path = f"/api/v1/batches/{batch_id}/items"
    items = api.get(path).json()["items"]
    return [api.get(f"/api/v1/sessions/{item['session_id']}").json()["batch_preset"] for item in items]


def test_fixed_preset_is_visible_before_browser_launch(db):
    with client() as api:
        response = api.post("/api/v1/batches", json=launch_payload(
            seed=9,
            batch_presets={"mode": "fixed", "presets": ["compact_390"]},
        ))
        assert response.status_code == 201, response.text
        assert session_assignments(api, response.json()["id"]) == [{
            "catalog_version": 1,
            "id": "compact_390",
            "application": "context",
            "device_class": "desktop",
            "viewport_class": "phone",
            "viewport": {"width": 390, "height": 844},
            "device_pixel_ratio": 3,
            "locale": "en-US",
            "timezone": "America/New_York",
            "mode": "fixed",
            "seed": 9,
        }]


@pytest.mark.parametrize("adapter", ["manual_chrome", "kameleo", "cloakbrowser"])
def test_adapter_viewport_assignment_is_visible_in_session_and_bundle(db, adapter):
    payload = launch_payload(
        adapters=[adapter], seed=9, batch_presets={"mode": "fixed", "presets": ["compact_390"]},
    )
    with client() as api:
        preflight = api.post("/api/v1/batches/preflight", json=payload)
        assert preflight.status_code == 200, preflight.text
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
        items = api.get(f"/api/v1/batches/{response.json()['id']}/items").raise_for_status().json()["items"]
        path = f"/api/v1/sessions/{items[0]['session_id']}"
        session = api.get(path).raise_for_status().json()
        bundle = api.get(f"{path}/diagnostic-bundle").raise_for_status().json()
    assert session["adapter"] == adapter
    assert session["batch_preset"] == bundle["session"]["batch_preset"] == {
        "catalog_version": 1,
        "id": "compact_390",
        "application": "viewport_only",
        "device_class": "adapter_managed",
        "viewport_class": "phone",
        "viewport": {"width": 390, "height": 844},
        "device_pixel_ratio": None,
        "locale": None,
        "timezone": None,
        "mode": "fixed",
        "seed": 9,
    }


def test_mixed_round_robin_uses_a_sequence_per_adapter(db):
    payload = launch_payload(
        seed=13,
        session_groups=[
            {"adapter": adapter, "count": 1 if adapter != "playwright" else 2}
            for _ in range(2)
            for adapter in ("playwright", "manual_chrome", "kameleo", "cloakbrowser")
        ],
        batch_presets={
            "mode": "round_robin", "presets": ["desktop_1440", "tablet_768"], "locales": ["en-GB", "he-IL"],
        },
    )
    with client() as api:
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
        assignments = session_assignments(api, response.json()["id"])
    assert [(entry["id"], entry["locale"], entry["application"]) for entry in assignments] == [
        ("desktop_1440", "en-GB", "context"),
        ("desktop_1440", "he-IL", "context"),
        *[("desktop_1440", None, "viewport_only")] * 3,
        ("tablet_768", "en-GB", "context"),
        ("tablet_768", "he-IL", "context"),
        *[("tablet_768", None, "viewport_only")] * 3,
    ]


def test_round_robin_uses_global_ordinals_across_profile_groups(db):
    with db.begin() as connection:
        connection.add(BrowserProfile(
            id="profile-qa", project_id="default", name="QA profile", adapter="playwright", persistence="persistent",
        ))
    payload = launch_payload(
        seed=13,
        session_groups=[
            {"adapter": "playwright", "count": 1, "profile_ids": ["profile-qa"]},
            {"adapter": "playwright", "count": 4},
        ],
        batch_presets={
            "catalog_version": 1, "mode": "round_robin",
            "presets": ["desktop_1440", "tablet_768"], "locales": ["en-GB", "he-IL"],
        },
    )
    with client() as api:
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
        batch_id = response.json()["id"]
        assignments = session_assignments(api, batch_id)
    assert [(assignment["id"], assignment["locale"], assignment["timezone"]) for assignment in assignments] == [
        ("desktop_1440", "en-GB", "Europe/London"),
        ("desktop_1440", "he-IL", "Asia/Jerusalem"),
        ("tablet_768", "en-GB", "Europe/London"),
        ("tablet_768", "he-IL", "Asia/Jerusalem"),
        ("desktop_1440", "en-GB", "Europe/London"),
    ]
    with db() as connection:
        rows = list(connection.scalars(select(BrowserSession).order_by(BrowserSession.ordinal)))
        assert rows[0].profile_id == "profile-qa" and rows[0].persistence == "persistent"
        assert len({row.profile_id for row in rows}) == 5


def test_seeded_assignment_replays_with_recorded_seed(db, monkeypatch):
    generated_seeds = iter((1147,))
    monkeypatch.setattr(platform_service.secrets, "randbelow", lambda _bound: next(generated_seeds))
    payload = launch_payload(count=16, batch_presets={
        "mode": "seeded", "presets": ["desktop_1280", "desktop_1440", "compact_390", "tablet_768"],
        "locales": ["en-US", "en-GB", "he-IL"],
    })
    with client() as api:
        response = api.post("/api/v1/batches", json=payload)
        assert response.status_code == 201, response.text
        first = response.json()
        assert first["config"]["seed"] == 1147
        assert api.post("/api/v1/batches", json=payload).json()["id"] == first["id"]
        changed = api.post("/api/v1/batches", json={**payload, "seed": 1148})
        assert changed.status_code == 409, changed.text
        replay = api.post("/api/v1/batches", json={
            **payload, "seed": first["config"]["seed"], "idempotency_key": "batch-presets-replay",
        })
        assert replay.status_code == 201, replay.text
        first_assignments = session_assignments(api, first["id"])
        assert first_assignments == session_assignments(api, replay.json()["id"])
    pairs = [(assignment["id"], assignment["locale"]) for assignment in first_assignments]
    assert len(set(pairs[:12])) == 12 and len(set(pairs[12:])) == 4
    assert pairs[:6] == [
        ("desktop_1280", "he-IL"),
        ("tablet_768", "he-IL"),
        ("compact_390", "he-IL"),
        ("desktop_1440", "en-US"),
        ("desktop_1280", "en-GB"),
        ("compact_390", "en-GB"),
    ]


def test_seeded_viewport_assignment_uses_reproducible_viewport_only_cycles(db):
    presets = ["desktop_1280", "compact_390", "tablet_768"]
    payload = launch_payload(
        count=6, seed=25, adapters=["kameleo"],
        batch_presets={"mode": "seeded", "presets": presets, "locales": ["en-GB", "he-IL"]},
    )
    with client() as api:
        first = api.post("/api/v1/batches", json=payload).raise_for_status().json()
        replay = api.post("/api/v1/batches", json={
            **payload, "idempotency_key": "viewport-presets-replay",
        }).raise_for_status().json()
        assignments = session_assignments(api, first["id"])
        assert assignments == session_assignments(api, replay["id"])
    selected = [entry["id"] for entry in assignments]
    assert set(selected[:3]) == set(selected[3:]) == set(presets)
    assert all(entry["application"] == "viewport_only" and entry["locale"] is None for entry in assignments)


@pytest.mark.parametrize("overrides", [
    {"user_agent": "custom"},
    {"device_preset": "phone"},
    {"session_groups": [{"adapter": "playwright", "user_agent": "custom"}]},
    {"session_groups": [{"adapter": "playwright", "device_preset": "phone"}]},
    {"adapters": ["cloakbrowser"], "device_preset": "phone"},
    {"session_groups": [{"adapter": "kameleo", "device_preset": "phone"}]},
])
def test_preflight_and_creation_reject_conflicting_browser_settings(db, overrides):
    payload = launch_payload(batch_presets={"mode": "fixed", "presets": ["desktop_1280"]}, **overrides)
    with client() as api:
        for path in ("/api/v1/batches/preflight", "/api/v1/batches"):
            response = api.post(path, json=payload)
            assert response.status_code == 422, response.text


@pytest.mark.parametrize("settings", [
    {"catalog_version": 2},
    {"presets": []},
    {"presets": ["desktop_1280", "desktop_1440"]},
    {"presets": ["unknown"]},
    {"mode": "seeded", "presets": ["desktop_1280", "desktop_1280"]},
    {"mode": "round_robin", "locales": ["en-US", "en-US"]},
    {"locales": []},
    {"locales": ["en-US", "en-GB"]},
    {"locales": ["unknown"]},
])
def test_preset_options_require_supported_unique_selections(db, settings):
    with client() as api:
        response = api.post("/api/v1/batches", json=launch_payload(batch_presets={
            "mode": "fixed", "presets": ["desktop_1280"], **settings,
        }))
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(("adapter", "pool"), [("playwright", "standard"), ("kameleo", "vendor")])
def test_frozen_preset_survives_allocation_and_startup_retry(db, monkeypatch, adapter, pool):
    monkeypatch.setattr(platform_service, "execution_capabilities", lambda: [{"id": adapter, "available": True}])
    with db.begin() as connection:
        connection.add(BrowserProfile(
            id="held-profile", project_id="default", name="Held profile",
            adapter=adapter, persistence="fresh", status="quarantined",
        ))
    with client() as api:
        response = api.post("/api/v1/batches", json=launch_payload(
            seed=16, adapters=[adapter], profile_ids=["held-profile"],
            batch_presets={"mode": "seeded", "presets": ["desktop_1280", "tablet_768"]},
        ))
        assert response.status_code == 201, response.text
        batch = response.json()
        assert batch["creation"] == {"failed": 1}
        with db.begin() as connection:
            item = connection.get(SessionBatchItem, (batch["id"], 1))
            frozen = item.launch_config["batch_preset"]
            connection.get(BrowserProfile, "held-profile").status = "available"
        retried = api.post(
            f"/api/v1/batches/{batch['id']}/retry-creation",
            headers={"Idempotency-Key": "retry-presets-allocation"},
        )
        assert retried.status_code == 200 and retried.json()["creation"] == {"created": 1}, retried.text
        assert session_assignments(api, batch["id"]) == [frozen]

    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim(pool)
    assert work is not None and work.config["batch_preset"] == frozen
    session_runner.finish(work, BrowserResult("failed", 1, "Transient startup failed", {
        "runtime_stopped": True, "failure_kind": "startup_transient",
    }, False))
    with db.begin() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == "queued" and row.queue_reason == "retry_backoff"
        row.next_attempt_at = platform_service.utc_after(-1)
        connection.get(Target, "target").last_started_at = None
        session_runner.admission_tick(connection)
    replacement = session_runner.claim(pool)
    assert replacement is not None and replacement.id == work.id
    assert replacement.attempt_number == work.attempt_number + 1
    assert replacement.profile_id == work.profile_id and replacement.config["batch_preset"] == frozen
