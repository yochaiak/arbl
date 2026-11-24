import json

import pytest
from sqlalchemy import select
from test_platform import client, create, data
from test_platform import db as db

from abrl.batch_presets import BatchPresetOptions, select_preset
from abrl.db import now
from abrl.diagnostic_compare import main as compare
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    ProxyInventory,
    ProxyLease,
    SessionAttempt,
    SessionBatch,
    SessionBatchItem,
)
from abrl.platform_service import create_batch


def _attempt(row, status, metadata, *, epoch=1):
    row.status = row.outcome = row.workflow_outcome = status
    row.epoch = epoch
    row.attempt_number = 1
    row.finished_at = now()
    row.metadata_json = {"environment_epoch": epoch, **metadata}


@pytest.mark.parametrize("application", ["context", "viewport_only"])
def test_frozen_preset_is_reported_before_launch_and_canonicalized_in_exports(db, application):
    batch = create(db, mode="live", count=2)
    options = BatchPresetOptions(mode="round_robin", presets=["compact_390"], locales=["en-GB"])
    expected = select_preset(options, ordinal=1, seed=9876, application=application)
    with db.begin() as connection:
        valid, invalid = connection.scalars(select(SessionBatchItem).where(
            SessionBatchItem.batch_id == batch.id,
        ).order_by(SessionBatchItem.ordinal)).all()
        stored = {
            **expected, "timezone": "private-unrecognized-timezone", "proxy_token": "private-token",
        }
        if application == "context":
            stored.pop("application")  # Historical Chromium-only assignments.
        valid.launch_config = {"batch_preset": stored}
        invalid.launch_config = {"batch_preset": {**expected, "locale": "private-invalid-locale"}}
        session = connection.get(BrowserSession, valid.session_id)
        session.metadata_json = {"batch_preset": {"private-key": "private-runtime-value"}}

    route = f"/api/v1/sessions/{valid.session_id}"
    with client() as api:
        detail = api.get(route).raise_for_status()
        exported = api.get(f"{route}/diagnostic-bundle").raise_for_status()
        other = api.get(f"/api/v1/sessions/{invalid.session_id}/diagnostic-bundle").raise_for_status()
        listed = api.get("/api/v1/sessions", params={"batch_id": batch.id}).raise_for_status()

    assert detail.json()["attempt_number"] == 0
    assert detail.json()["batch_preset"] == exported.json()["session"]["batch_preset"] == expected
    assert other.json()["session"]["batch_preset"] is None
    assert {row["id"]: row["batch_preset"] for row in listed.json()["items"]} == {
        valid.session_id: expected, invalid.session_id: None,
    }
    assert all("private-" not in response.text for response in (detail, exported, other, listed))


def test_diagnostic_bundle_sanitizes_both_reports_and_supports_automatic_and_explicit_baselines(db):
    batch = create(db, mode="live", count=2)
    with db.begin() as connection:
        flagged, clean = connection.scalars(select(BrowserSession).where(
            BrowserSession.batch_id == batch.id,
        ).order_by(BrowserSession.ordinal)).all()
        proxy = ProxyInventory(project_id="default", name="Fixture", config={"host": "private-gateway.example"})
        connection.add(proxy)
        connection.flush()
        lease = ProxyLease(session_id=flagged.id, inventory_id=proxy.id, epoch=1, status="released", expires_at=now(),
                           config={"product": "residential", "session_key": "PrivateStickyToken", "password": "private-password"},
                           rationale={})
        connection.add(lease)
        connection.flush()
        flagged.proxy_lease_id = lease.id
        connection.add(SessionAttempt(id="bundle-fixture-attempt", session_id=flagged.id, number=1, epoch=1,
                                      proxy_lease_id=lease.id, proxy_check_ip="192.0.2.71"))
        raw = {"headers": {"Authorization": "Bearer private-auth-token", "Cookie": "private-cookie"},
               "ice": {"candidate": "private-ice-candidate"}, "ip": "192.0.2.72"}
        _attempt(clean, "completed", {
            "browser_diagnostic": {"status": "partial", "epoch": 1, "attempt": 1, "observations": raw},
        })
        _attempt(flagged, "blocked", {
            "proxy_preflight": {"status": "passed", "attempt": 1, "epoch": 1, "checks": {
                "country": {"status": "passed", "confirmed": True, "observed": "US"},
                "asn": {"status": "passed", "confirmed": True, "observed": 64500},
                "network_type": {"status": "passed", "source": "maxmind", "observed": "residential"},
                "exit_ip": {"observed": "192.0.2.73"},
                "sticky": {"status": "passed"},
            }},
            "lifecycle_timeline": {"version": 1, "events": [
                {"event": "navigation_started", "elapsed_ms": 20},
                {"event": "failure_observed", "elapsed_ms": 50, "url": "private-target.example"},
            ]},
            "navigation_connects": {"version": 1, "truncated": False, "navigations": [{
                "navigation": 2, "step": 1, "redirected": True, "outcome": "failed", "attribution": "observed",
                "destination": "private-redirect.example",
                "connects": [
                    {"source": "decodo", "reason": "proxy_tunnel_failed", "http_status": 502,
                     "provider_text": "private-provider-response", "ip": "192.0.2.74"},
                    {"source": "private-gateway.example", "reason": "private-provider-reason", "http_status": 400},
                ],
            }]},
            "browser_diagnostic": {
                "status": "failed", "attempt": 1, "epoch": 1, "observations": raw,
                "checks": {"user_agent": "mismatch", "viewport": "passed"},
                "fixture_http_protocols": {"document": "h2"},
                "capabilities": {"audio_context": "unsupported"},
            },
            "causal_failure": {"reason": "site_challenge", "phase": "workflow", "step": 1, "error": "private-error"},
            **raw,
        })
        flagged.error = "private-error"
    route = f"/api/v1/sessions/{flagged.id}/diagnostic-bundle"
    with client() as api:
        response = api.get(route, params={"auto_baseline": "true"})
        explicit = api.get(route, params={"baseline_id": clean.id[:12]})
        single = api.get(route)
        unauthorized = api.get(route, headers={"Authorization": "Bearer wrong"})
        detail = api.get(f"/api/v1/sessions/{flagged.id}")

    assert response.status_code == explicit.status_code == single.status_code == 200
    assert unauthorized.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["content-disposition"] == 'attachment; filename="abrl-diagnostic-bundle.json"'
    bundle = response.json()
    assert bundle["schema"] == "abrl.diagnostic_bundle" and bundle["version"] == 1
    assert bundle["baseline_selection"] == "automatic"
    assert bundle["baseline_evidence"] == "partial"
    assert bundle["baseline"]["session_id"] == clean.id
    assert explicit.json()["baseline_selection"] == "explicit"
    assert explicit.json()["baseline"] == bundle["baseline"]
    assert single.json()["baseline"] is None and single.json()["baseline_selection"] == "not_requested"
    record = bundle["session"]
    assert record["session_id"] == flagged.id
    network = record["proxy_network"]
    assert network["exit_lookup"] == {
        "country": "US", "asn": 64500, "network_classification": "residential", "preflight_status": "passed",
        "preflight_reason": None,
        "evidence": {
            field: {"status": "passed", "reason": None, "dataset_status": None}
            for field in ("country", "asn", "network_classification")
        },
    }
    assert network["proxy"]["continuity"] == "sticky" and network["proxy"]["sticky_check"] == "passed"
    assert network["negotiated_http"]["document"] == "h2"
    assert record["browser_diagnostics"]["mismatches"] == ["user_agent"]
    assert record["browser_diagnostics"]["unsupported_capabilities"] == ["audio_context"]
    assert record["lifecycle_outcome"]["first_causal_failure"]["reason"] == "site_challenge"
    assert record["lifecycle_outcome"]["timeline"]["events"][-1]["navigation_delta_ms"] == 30
    connects = {"version": 1, "truncated": False, "navigations": [{
        "navigation": 2, "step": 1, "redirected": True, "outcome": "failed", "attribution": "observed",
        "connects": [{"source": "decodo", "reason": "proxy_tunnel_failed", "http_status": 502}],
    }]}
    assert record["lifecycle_outcome"]["navigation_connects"] == connects
    assert bundle["baseline"]["lifecycle_outcome"]["navigation_connects"] is None
    assert detail.status_code == 200 and detail.json()["metadata"]["navigation_connects"] == connects
    assert all(value not in response.text for value in (
        "PrivateStickyToken", "private-", "192.0.2.", "session_key", "Authorization", "Cookie", "candidate",
    ))


def test_diagnostic_bundle_scopes_baseline_selection_and_filters_stale_attempt_evidence(db):
    batch = create(db, mode="live")
    with client() as api:
        project = api.post("/api/v1/projects", json={"name": "Other project"}).raise_for_status().json()["id"]
    with db.begin() as connection:
        selected = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == batch.id))
        _attempt(selected, "failed", {
            "environment_epoch": 1,
            "proxy_preflight": {"status": "passed", "checks": {"asn": {
                "status": "passed", "confirmed": True, "observed": 65535,
            }}},
            "lifecycle_timeline": {"version": 1, "events": [{"event": "browser_launched", "elapsed_ms": 100}]},
            "navigation_connects": {"version": 1, "truncated": False, "navigations": [{
                "navigation": 1, "redirected": False, "outcome": "failed", "attribution": "observed",
                "connects": [{"source": "local_proxy", "reason": "local_dns_denied", "http_status": 403}],
            }]},
        }, epoch=2)
        other_batch = SessionBatch(project_id=project, name="Other", idempotency_key="other-fixture",
                                   request_hash="", config={}, count=1, concurrency=1)
        other_profile = BrowserProfile(project_id=project, name="Other", adapter="playwright", persistence="fresh")
        connection.add_all([other_batch, other_profile])
        connection.flush()
        other = BrowserSession(project_id=project, batch_id=other_batch.id, profile_id=other_profile.id,
                               adapter="playwright", persistence="fresh", mode="live", pool="standard", ordinal=1)
        _attempt(other, "completed", {})
        connection.add(other)
    route = f"/api/v1/sessions/{selected.id}/diagnostic-bundle"
    with client() as api:
        response = api.get(route, params={"auto_baseline": "true"})
        detail = api.get(f"/api/v1/sessions/{selected.id}")
        assert api.get(route, params={"baseline_id": other.id}).status_code == 404
        assert api.get(route, headers={"X-Project-ID": project}).status_code == 404
        assert api.get(route, params={"baseline_id": selected.id}).status_code == 409
        invalid = api.get(route, params={"baseline_id": "private-invalid-id"})
        assert invalid.status_code == 422 and "private-invalid-id" not in invalid.text
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["baseline"] is None and report["baseline_selection"] == "unavailable"
    assert report["session"]["proxy_network"]["exit_lookup"]["asn"] is None
    assert report["session"]["lifecycle_outcome"]["timeline"] is None
    assert report["session"]["lifecycle_outcome"]["navigation_connects"] is None
    assert detail.status_code == 200 and "navigation_connects" not in detail.json()["metadata"]


@pytest.mark.parametrize("probed", [True, False])
def test_automatic_baseline_does_not_pick_a_report_with_a_known_failure(db, probed):
    batch = create(db, mode="live", count=2)
    with db.begin() as connection:
        selected, other = connection.scalars(select(BrowserSession).where(BrowserSession.batch_id == batch.id)).all()
        _attempt(selected, "blocked", {})
        metadata = {"browser_diagnostic": {"status": "failed", "epoch": 1, "attempt": 1}} if probed else {
            "causal_failure": {"reason": "site_challenge"},
        }
        _attempt(other, "completed", metadata)
    with client() as api:
        response = api.get(f"/api/v1/sessions/{selected.id}/diagnostic-bundle", params={"auto_baseline": "true"})
    assert response.status_code == 200, response.text
    assert response.json()["baseline_selection"] == "unavailable"


@pytest.mark.parametrize("difference", [None, "workflow_version", "timeout_seconds", "batch_preset"])
def test_bundle_attaches_a_passing_run_from_another_batch_with_matching_pinned_context(db, difference):
    flagged_batch = create(db, mode="live")
    with db.begin() as connection:
        baseline_batch = create_batch(connection, data(mode="live").model_copy(update={
            "idempotency_key": "passing-cross-batch-baseline",
        }), "default")
        if difference in ("workflow_version", "timeout_seconds"):
            baseline_batch.config = {**baseline_batch.config, difference: baseline_batch.config[difference] + 1}
        flagged = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == flagged_batch.id))
        baseline = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == baseline_batch.id))
        for row, mode, seed in ((flagged, "fixed", 42), (baseline, "seeded", 51)):
            item = connection.get(SessionBatchItem, (row.batch_id, row.ordinal))
            preset = "compact_390" if row.id == baseline.id and difference == "batch_preset" else "desktop_1280"
            item.launch_config = {"batch_preset": select_preset(
                BatchPresetOptions(mode=mode, presets=[preset]), ordinal=row.ordinal, seed=seed,
            )}
        _attempt(flagged, "failed", {"causal_failure": {"phase": "workflow", "reason": "navigation_timeout"}})
        _attempt(baseline, "completed", {"browser_diagnostic": {"status": "passed", "epoch": 1, "attempt": 1}})

    endpoint = f"/api/v1/sessions/{flagged.id}/diagnostic-bundle"
    with client() as api:
        auto = api.get(endpoint, params={"auto_baseline": "true"}).raise_for_status().json()
        explicit = api.get(endpoint, params={"baseline_id": baseline.id}).raise_for_status().json()
    assert explicit["baseline"]["session_id"] == baseline.id
    assert explicit["baseline"]["browser_diagnostics"]["fixture_status"] == "passed"
    assert explicit["session"]["lifecycle_outcome"]["first_causal_failure"]["reason"] == "navigation_timeout"
    if difference:
        assert auto["baseline"] is None and auto["baseline_selection"] == "unavailable"
    else:
        assert auto["baseline"] == explicit["baseline"] and auto["baseline_selection"] == "automatic"


def test_passing_baseline_takes_priority_and_pairs_with_timeout_in_offline_comparison(db, tmp_path, capsys):
    batch = create(db, mode="live", count=3)
    with db.begin() as connection:
        selected, partial, stale = connection.scalars(select(BrowserSession).where(
            BrowserSession.batch_id == batch.id,
        ).order_by(BrowserSession.ordinal)).all()
        passing_batch = create_batch(connection, data(mode="live").model_copy(update={
            "idempotency_key": "passing-priority-baseline",
        }), "default")
        passed = connection.scalar(select(BrowserSession).where(BrowserSession.batch_id == passing_batch.id))
        _attempt(passed, "completed", {"browser_diagnostic": {"status": "passed", "epoch": 1, "attempt": 1}})
        _attempt(partial, "completed", {"browser_diagnostic": {"status": "partial", "epoch": 1, "attempt": 1}})
        _attempt(stale, "completed", {"browser_diagnostic": {"status": "passed", "epoch": 1, "attempt": 1}}, epoch=2)
        _attempt(selected, "failed", {"causal_failure": {
            "reason": "navigation_timeout", "phase": "workflow", "navigation_wait": {
                "stage": "initial_dom_content_loaded", "deadline": "step", "load_timeout_ms": 5000,
                "url": "https://private-evidence.example/session",
            },
        }})
    endpoint = f"/api/v1/sessions/{selected.id}/diagnostic-bundle"
    with client() as api:
        automatic = api.get(endpoint, params={"auto_baseline": "true"}).raise_for_status()
        partial_result = api.get(endpoint, params={"baseline_id": partial.id}).raise_for_status().json()
        assert api.get(endpoint, params={"baseline_id": stale.id}).status_code == 409
    assert automatic.json()["baseline"]["session_id"] == passed.id
    assert automatic.json()["baseline_evidence"] == "passed"
    assert partial_result["baseline_evidence"] == "partial"
    path = tmp_path / "bundle.json"
    path.write_bytes(automatic.content)
    assert compare([str(path)]) == 0
    stdout = capsys.readouterr().out
    report = json.loads(stdout)
    assert report["outcomes"]["diagnostic"]["baseline"] == "passed"
    assert report["outcomes"]["failure_reason"]["candidate"] == "navigation_timeout"
    assert report["navigation_wait"] == {
        "baseline": None,
        "candidate": {"stage": "initial_dom_content_loaded", "deadline": "step", "load_timeout_ms": 5000},
    }
    assert "private-evidence" not in stdout and "private-evidence" not in automatic.text
