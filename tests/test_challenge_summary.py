import pytest
from sqlalchemy import select
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel
from test_platform import client, create
from test_platform import db as db
from test_proxy_platform import proxy_runtime as proxy_runtime
from test_proxy_platform import register_and_claim

from abrl import platform_service, proxy_preflight, session_runner
from abrl.browser import BrowserResult, execute_workflow
from abrl.db import now
from abrl.platform_models import BrowserSession, ProxyInventory, ProxyLease


def test_batch_challenge_summary_uses_separate_challenge_and_transport_denominators(db, monkeypatch):
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    batch = create(db, mode="live", count=8)
    verified = {
        "checks": {
            "asn": {"status": "passed", "confirmed": True, "observed": 64501},
            "country": {"status": "passed", "confirmed": True, "observed": "US"},
            "sticky": {"status": "passed"},
            "exit_ip": {"observed": "192.0.2.42"},
        }
    }
    unverified = {
        "checks": {
            "asn": {"status": "passed", "confirmed": True, "observed": 64502},
            "country": {"status": "passed", "confirmed": True, "observed": "CA"},
            "sticky": {"status": "failed"},
        }
    }
    with db.begin() as conn:
        connection_config = {"host": "private.proxy.example", "country": "GB", "password": "private-password"}
        pool = ProxyInventory(project_id="default", name="First proxy", config=connection_config)
        other_pool = ProxyInventory(project_id="default", name="Second proxy", config=connection_config)
        conn.add_all((pool, other_pool))
        conn.flush()
        challenged, completed, denied, historical, startup, fenced, demo, queued = conn.scalars(
            select(BrowserSession).where(BrowserSession.batch_id == batch.id).order_by(BrowserSession.ordinal)
        ).all()
        for session, status, inventory, metadata in (
            (challenged, "blocked", pool, {
                "proxy_preflight": verified,
                "causal_failure": {"reason": "site_challenge", "extra": "private-selector"},
                "transport_summary": {"successes": 1, "failures": 1, "latency_ms": 120},
            }),
            (completed, "completed", pool, {
                "proxy_preflight": verified,
                "authorized_document_observed": True,
                "transport_summary": {"successes": 3, "failures": 0, "latency_ms": 40},
            }),
            (denied, "blocked", other_pool, {
                "proxy_preflight": unverified,
                "authorized_document_observed": True,
                "causal_failure": {"reason": "site_denial", "status": 403},
            }),
            (historical, "completed", None, {}),
            (startup, "failed", pool, {
                "causal_failure": {"reason": "browser_crash"},
                "transport_summary": {"successes": 0, "failures": 2, "latency_ms": None},
            }),
            (fenced, "failed", other_pool, {
                "environment_epoch": 0,
                "causal_failure": {"reason": "site_challenge"},
                "proxy_preflight": verified,
                "transport_summary": {"successes": 0, "failures": 30, "latency_ms": None},
            }),
            (demo, "completed", None, {"authorized_document_observed": True}),
            (queued, "queued", None, {"causal_failure": {"reason": "site_challenge"}}),
        ):
            session.status = session.workflow_outcome = status
            session.finished_at = None if status == "queued" else now()
            session.epoch = 1
            session.metadata_json = {"environment_epoch": session.epoch, **metadata}
            if inventory is not None:
                lease = ProxyLease(
                    session_id=session.id, inventory_id=inventory.id, epoch=session.epoch,
                    config=connection_config, rationale={}, status="released", expires_at=now(),
                )
                conn.add(lease)
                conn.flush()
                session.proxy_lease_id = lease.id
        demo.mode = "demo"

    endpoint = f"/api/v1/batches/{batch.id}/challenge-summary"
    with client() as api:
        response = api.get(endpoint)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        other_project = api.post("/api/v1/projects", json={"name": "Other project"})
        assert other_project.status_code == 201, other_project.text
        assert api.get(endpoint, headers={"X-Project-Id": other_project.json()["id"]}).status_code == 404

    report = response.json()
    assert report["overall"] == {
        "finished_sessions": 6,
        "evaluated_sessions": 2,
        "unevaluated_sessions": 4,
        "unevaluated_reasons": {
            "completed_without_document_evidence": 1,
            "site_denial": 1,
            "stale_attempt_evidence": 1,
            "workflow_incomplete": 1,
        },
        "challenged_sessions": 1,
        "challenge_rate": 0.5,
        "transport_attempts": 7,
        "transport_errors": 3,
        "transport_error_rate": 3 / 7,
        "mean_transport_latency_ms": 60,
        "transport_latency_samples": 4,
    }
    by_pool = {group["value"]: group for group in report["by_pool"]}
    assert by_pool[pool.id]["challenge_rate"] == 0.5
    assert by_pool[other_pool.id]["challenge_rate"] is None
    assert by_pool[None]["unevaluated_sessions"] == 1
    assert [group["value"] for group in report["by_asn"]] == [64501, None]
    assert [group["value"] for group in report["by_country"]] == ["US", None]
    assert report["by_country"][0]["challenge_rate"] == 0.5
    assert report["by_country"][1]["evaluated_sessions"] == 0
    assert all(value not in response.text for value in (
        "private.proxy.example", "private-password", "192.0.2.42", "private-selector", "GB", "CA", challenged.id,
    ))


def test_unevaluated_reasons_and_probe_evidence_are_redacted_and_independent(db, monkeypatch):
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    batch = create(db, mode="live", count=7)
    scenarios = (
        ("completed", "completed", {
            "authorized_document_observed": True,
            "proxy_preflight": {
                "status": "partial",
                "checks": {
                    "exit_ip": {"status": "passed", "observed": "192.0.2.93"},
                    "country": {"status": "passed", "confirmed": True, "observed": "US"},
                    "asn": {"status": "unavailable", "confirmed": False,
                            "dataset_status": "/private-dataset-path.mmdb"},
                    "target_tls": {"status": "failed", "reason": "origin_tls_timeout"},
                },
                "probe_attempts": {"lookup": [{"reason": "lookup_response_timeout"}, {"reason": None}]},
            },
            "browser_diagnostic": {
                "status": "partial",
                "checks": {
                    "webgl": "unavailable", "ice": "inconclusive", "expected_language": "not_configured",
                    "private_browser_field": "unavailable",
                },
                "observations": {"ice": "private_candidate", "cookies": "private_cookie"},
            },
        }),
        ("blocked", "not_started", {
            "proxy_preflight": {
                "status": "blocked",
                "checks": {
                    "exit_ip": {"status": "passed"},
                    "country": {"status": "failed", "confirmed": True, "reason": "country_mismatch"},
                },
            },
            "browser_diagnostic": {"status": "failed", "reason": "private_diagnostic_reason"},
        }),
        ("failed", "failed", {
            "causal_failure": {"reason": "navigation_failed"},
            "step_failure": {"reason": "proxy_tunnel_timeout", "error": "private_password"},
        }),
        ("failed", "failed", {"causal_failure": {"reason": "navigation_timeout"}}),
        ("cancelled", "not_started", {}),
        ("blocked", "not_started", {}),
        ("failed", "failed", {
            "environment_epoch": None,
            "proxy_preflight": {"status": "unavailable", "reason": "preflight_timeout"},
            "browser_diagnostic": {"status": "partial", "checks": {"webgl": "unavailable"}},
        }),
    )
    with db.begin() as conn:
        sessions = conn.scalars(
            select(BrowserSession).where(BrowserSession.batch_id == batch.id).order_by(BrowserSession.ordinal)
        ).all()
        for session, (status, workflow_outcome, metadata) in zip(sessions, scenarios, strict=True):
            session.status = status
            session.workflow_outcome = workflow_outcome
            session.epoch = 1
            session.finished_at = now()
            session.metadata_json = {"environment_epoch": session.epoch, **metadata}

    with client() as api:
        response = api.get(f"/api/v1/batches/{batch.id}/challenge-summary")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["overall"]["evaluated_sessions"] == 1
    assert report["overall"]["challenge_rate"] == 0
    assert report["overall"]["unevaluated_reasons"] == {
        "cancelled": 1,
        "missing_finalized_evidence": 1,
        "preflight_blocked": 1,
        "transport_timeout": 1,
        "workflow_not_started": 1,
        "workflow_timeout": 1,
    }
    assert report["diagnostics"] == {
        "preflight_reports": 2,
        "preflight_timeout_sessions": 1,
        "missing_lookup_fields": {"asn": 2, "network_type": 2, "timezone": 2},
        "missing_lookup_dataset_statuses": {},
        "browser_reports": 2,
        "browser_reports_without_checks": 1,
        "browser_unavailable_or_inconclusive_fields": {"ice": 1, "webgl": 1},
    }
    metrics = [report["overall"], *report["by_pool"], *report["by_asn"], *report["by_country"]]
    assert all(sum(group["unevaluated_reasons"].values()) == group["unevaluated_sessions"] for group in metrics)
    assert all(value not in response.text for value in (
        "192.0.2.93", "private_password", "private_candidate", "private_cookie", "private_browser_field",
        "private_diagnostic_reason", "/private-dataset-path.mmdb",
    ))


@pytest.mark.parametrize("dataset_status", ["not_configured", "missing_or_not_file"])
@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_decodo_session_reports_absent_datasets_without_blocking_the_workflow(
    db, proxy_runtime, monkeypatch, tmp_path, caplog, dataset_status, outcome,
):
    work, _ = register_and_claim(db, provider="decodo", continuity="sticky")
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path)
    missing_dataset = tmp_path / "private-dataset.mmdb"
    for setting in ("ABRL_GEOIP_CITY_DB", "ABRL_GEOIP_ASN_DB", "ABRL_GEOIP_ANONYMOUS_DB"):
        if dataset_status == "not_configured":
            monkeypatch.delenv(setting, raising=False)
        else:
            monkeypatch.setenv(setting, str(missing_dataset))

    exit_address = "203.0.113.42"
    calls = []

    async def probe(binding, origin, timeout, path):
        assert binding.provider == "decodo" and binding.continuity == "sticky"
        assert 0 < timeout <= proxy_preflight.PROBE_TIMEOUT_SECONDS
        calls.append((origin, path))
        if path is None:
            return proxy_preflight._Probe(timings={"tunnel_establish": 75})
        return proxy_preflight._Probe(
            ip=exit_address,
            metadata={"country": "CA", "asn": 64512, "timezone": "America/Toronto"},
            timings={"tunnel_establish": 60},
        )

    async def run_browser(**settings):
        assert settings["origin"] == "https://authorized.example"
        assert calls == [
            ("https://ip.decodo.com", "/json"), ("https://ip.decodo.com", "/json"),
            ("https://authorized.example", None),
        ]
        metadata = {"runtime_stopped": True, "authorized_document_observed": True}
        if outcome == "failed":
            metadata["causal_failure"] = {"reason": "navigation_timeout", "phase": "workflow"}
        return BrowserResult(outcome, 30, None, metadata, True)

    async def check_proxy_ip(_):
        return exit_address

    monkeypatch.setattr(proxy_preflight, "_probe", probe)
    monkeypatch.setattr(session_runner, "execute_workflow", run_browser)
    monkeypatch.setattr(session_runner, "check_proxy_ip", check_proxy_ip)
    await session_runner.execute(work)

    with db() as conn:
        session = conn.get(BrowserSession, work.id)
        assert session.status == outcome
        preflight = session.metadata_json["proxy_preflight"]
        assert preflight["status"] == "partial" and not preflight["blocked"]
        assert preflight["reason"] == "unconfirmed_provider_evidence"
        assert preflight["checks"]["sticky"] == preflight["checks"]["target_tls"] == {"status": "passed"}
        batch_id = session.batch_id

    with client() as api:
        summary_response = api.get(f"/api/v1/batches/{batch_id}/challenge-summary")
        bundle_response = api.get(f"/api/v1/sessions/{work.id}/diagnostic-bundle")
    assert summary_response.status_code == bundle_response.status_code == 200
    summary = summary_response.json()
    assert summary["overall"]["evaluated_sessions"] == int(outcome == "completed")
    assert summary["overall"]["unevaluated_reasons"] == ({"workflow_timeout": 1} if outcome == "failed" else {})
    missing = ("country", "asn", "network_type", "timezone")
    assert summary["diagnostics"]["missing_lookup_fields"] == dict.fromkeys(missing, 1)
    assert summary["diagnostics"]["missing_lookup_dataset_statuses"] == {
        name: {dataset_status: 1} for name in missing
    }
    assert summary["by_asn"][0]["value"] is summary["by_country"][0]["value"] is None
    lookup = bundle_response.json()["session"]["proxy_network"]["exit_lookup"]
    assert lookup["country"] is lookup["asn"] is lookup["network_classification"] is None
    assert lookup["evidence"] == {
        name: {"status": "unavailable", "reason": reason, "dataset_status": dataset_status}
        for name, reason in (("country", "unconfirmed_provider_evidence"), ("asn", "unconfirmed_provider_evidence"),
                             ("network_classification", "evidence_unavailable"))
    }
    assert all(value not in summary_response.text + bundle_response.text for value in (
        exit_address, str(missing_dataset), "America/Toronto", "64512",
    ))
    assert not any(record.name in {"abrl.sessions", proxy_preflight.__name__} for record in caplog.records)


def test_finished_challenge_is_observational_and_does_not_change_proxy_health(db, proxy_runtime):
    work, pool_id = register_and_claim(db)
    result = BrowserResult("blocked", 20, "Site requested interactive verification", {
        "runtime_stopped": True,
        "failure_kind": "site_denial",
        "causal_failure": {"reason": "site_challenge"},
    }, True)
    session_runner.finish(work, result)

    with db() as conn:
        session = conn.get(BrowserSession, work.id)
        assert session.status == "blocked" and session.finished_at is not None
        assert conn.get(ProxyLease, session.proxy_lease_id).status == "released"
        batch_id = session.batch_id
        pool = conn.get(ProxyInventory, pool_id)
        assert pool.enabled and pool.health == {}

    with client() as api:
        response = api.get(f"/api/v1/batches/{batch_id}/challenge-summary")
    assert response.status_code == 200, response.text
    assert response.json()["overall"]["challenge_rate"] == 1


@pytest.mark.integration
@pytest.mark.parametrize(("path", "outcome", "observed"), [
    ("/lab", "completed", True),
    ("/lab/form?challenge", "blocked", True),
    ("/lab/next", "blocked", None),
])
async def test_authorized_document_evidence_is_recorded_only_for_successful_document_responses(
    fixture, chromium, path, outcome, observed,
):
    origin, _, _ = fixture
    result = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel,
        steps=[{"action": "navigate", "url": path}],
    )
    assert result.outcome == outcome, result.error
    assert result.metadata.get("authorized_document_observed") is observed
