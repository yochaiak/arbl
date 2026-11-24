import base64
import json
from types import SimpleNamespace

from sqlalchemy import select
from test_platform import client, create
from test_platform import db as db

from abrl import platform_service, session_outbox, session_runner
from abrl.browser import BrowserResult
from abrl.platform_models import BrowserSession, SessionAttempt, SessionEvent
from abrl.reports import trial_view

_INTERNAL_METADATA = {
    "cdp_endpoint": "ws://localhost/private-debug-endpoint",
    "vendor_profile_id": "private-vendor-profile",
    "correlation": "private-correlation",
    "browser_version": "Chromium/156.0",
    "proxy": {
        "provider": "decodo",
        "username_ref": "PRIVATE_USERNAME_REF",
        "password": "private-proxy-password",
        "headers": [{"name": "Authorization", "value": "private-auth-header"}],
    },
    "launch_details": [
        {"proxyToken": "private-proxy-token", "state": "configured"},
        "https://anonymous:private-password@fixture.example",
        "https://fixture.example/diagnostics?api-key=private-api-key",
    ],
}


def test_historical_session_public_projection_keeps_authenticated_viewer(db, monkeypatch):
    batch = create(db)
    frame = b"fixture-frame"
    historical = {**_INTERNAL_METADATA, "viewer_frame": base64.b64encode(frame).decode()}
    with db.begin() as connection:
        row = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == batch.id))
        row.status = "awaiting_operator"
        row.metadata_json = historical
        row.error = "Adapter failed at ws://localhost/private-debug-endpoint"
        session_id = row.id
        connection.add(SessionEvent(
            project_id=row.project_id, session_id=row.id, kind="historical_fixture", detail=historical,
        ))

    route = f"/api/v1/sessions/{session_id}"
    with client() as api:
        detail = api.get(route).raise_for_status()
        listed = api.get("/api/v1/sessions", params={"batch_id": batch.id}).raise_for_status()
        events = api.get(f"{route}/events").raise_for_status()
        exported = api.get(f"{route}/diagnostic-bundle").raise_for_status()
        for response in (detail, listed, events, exported):
            assert "private-" not in response.text
            assert "PRIVATE_USERNAME_REF" not in response.text
            assert historical["viewer_frame"] not in response.text
        assert detail.json()["error"] == "[redacted]"
        assert detail.json()["metadata"]["browser_version"] == "Chromium/156.0"
        assert detail.json()["metadata"]["proxy"] == {"provider": "decodo"}
        assert detail.json()["metadata"]["launch_details"] == [
            {"state": "configured"}, "[redacted]", "[redacted]",
        ]
        assert api.get(f"{route}/frame").raise_for_status().content == frame

    sent = []
    monkeypatch.setattr(session_outbox, "Session", db)
    monkeypatch.setattr(session_outbox, "CLICKHOUSE_URL", "http://fixture.invalid")

    def forward(url, *, content, **kwargs):
        sent.extend(json.loads(line) for line in content.splitlines())
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(session_outbox.httpx, "post", forward)
    session_outbox.forward_session_events()
    forwarded = next(entry for entry in sent if entry["kind"] == "historical_fixture")
    assert "private-" not in forwarded["detail"] and historical["viewer_frame"] not in forwarded["detail"]
    assert json.loads(forwarded["detail"])["browser_version"] == "Chromium/156.0"
    with db() as connection:
        assert connection.get(BrowserSession, session_id).metadata_json == historical


def test_session_write_redacts_adapter_internals_and_scopes_viewport_evidence(db, monkeypatch):
    monkeypatch.setattr(platform_service, "execution_capabilities", lambda: [
        {"id": "playwright", "available": True},
    ])
    create(db, batch_presets={"presets": ["desktop_1280"]})
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None

    with db.begin() as connection:
        row = connection.get(BrowserSession, work.id)
        row.metadata_json = {
            "environment_epoch": work.epoch - 1,
            "viewport_conformance": {"checks": 9, "matched": 9},
        }

    route = f"/api/v1/sessions/{work.id}"
    with client() as api:
        before = api.get(route).raise_for_status().json()
        assert before["batch_preset"]["id"] == "desktop_1280"
        assert "viewport_conformance" not in before["metadata"]
        assert api.get(f"{route}/diagnostic-bundle").raise_for_status().json()[
            "session"
        ]["browser_diagnostics"]["viewport_conformance"] is None

        session_runner.record_activity(work, {
            "kind": "launched", **_INTERNAL_METADATA,
            "viewport_conformance": {"checks": 1, "matched": 1, "description": "private-detail"},
        })
        session_runner.record_activity(work, {
            "kind": "operator_step_reserved", "step": 2, "action": "fill", "step_path": [1, "then", 2],
            "description": "private-user-input",
        })
        during = api.get(route).raise_for_status().json()["metadata"]
        assert during["environment_epoch"] == work.epoch - 1
        assert during["viewport_conformance"] == {"checks": 1, "matched": 1}
        assert "viewport_conformance_epoch" not in during
        report = api.get(f"{route}/diagnostic-bundle").raise_for_status().json()["session"]
        assert report["browser_diagnostics"]["viewport_conformance"] == during["viewport_conformance"]

    result = BrowserResult("failed", 50, "Proxy error: Authorization: Basic private-auth", {
        **_INTERNAL_METADATA, "runtime_stopped": True, "viewport_conformance": {"checks": 2, "matched": 1},
    }, False)
    session_runner.finish(work, result)
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.metadata_json["runtime_stopped"] is True
        assert row.metadata_json["viewport_conformance"] == {"checks": 2, "matched": 1}
        assert "private-" not in json.dumps(row.metadata_json)
        assert connection.get(SessionAttempt, work.lease).error == "[redacted]"
        events = {
            event.kind: event.detail for event in connection.scalars(
                select(SessionEvent).where(SessionEvent.session_id == work.id)
            )
        }
        assert "private-" not in json.dumps(events)
        assert events["operator_step_reserved"] == {"step": 2, "action": "fill", "step_path": [1, "then", 2]}
        assert events["launched"]["viewport_conformance"] == {"checks": 1, "matched": 1}


def test_legacy_trial_view_redacts_historical_adapter_internals():
    row = SimpleNamespace(
        id="trial", ordinal=1, status="finished", outcome="failed", elapsed_ms=1,
        error="Could not reach wss://fixture.example/private-endpoint", attempts=1,
        metadata_json=_INTERNAL_METADATA,
    )
    result = trial_view(row)
    assert "private-" not in json.dumps(result)
    assert result["error"] == "[redacted]"
    assert result["environment"]["proxy"] == {"provider": "decodo"}
