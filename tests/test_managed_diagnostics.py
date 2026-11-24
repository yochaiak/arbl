import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_platform import client
from test_platform import db as db
from test_proxy_platform import proxy_runtime as proxy_runtime

from abrl import session_runner
from abrl.browser import BrowserResult
from abrl.browser_diagnostics import diagnostic_report
from abrl.diagnostic_fixture import DIAGNOSTIC_PATH
from abrl.platform_models import BrowserSession, ProxyInventory, ProxyLease, SessionArtifact, SessionAttempt

ORIGIN = "https://qa.owned-fixture.dev"
RAW_OBSERVATION = "private-browser-observation 198.51.100.25"


def observation_rows():
    epoch_ms = 1_798_000_000_000
    groups = [
        {"server_ua": RAW_OBSERVATION, "browser_ua": RAW_OBSERVATION,
         "platform": None, "webdriver": True, "proxy_header_names": []},
        {"server_brands": None, "server_full_versions": None, "server_mobile": None, "server_platform": None,
         "server_platform_version": None, "browser_brands": [], "browser_full_versions": False,
         "browser_mobile": None, "browser_platform": None, "browser_platform_version": None},
        {"accept_language": "en-US,en;q=0.9", "language": "en-US", "languages": ["en-US", "en"],
         "timezone": "UTC", "offset_minutes": 0, "before_epoch_ms": epoch_ms, "after_epoch_ms": epoch_ms + 2,
         "server_epoch_ms": epoch_ms + 1},
        {"inner": [640, 480], "screen": [640, 480], "dpr": 1, "webgl": None,
         "document_protocol": "h2", "echo_protocol": "h2",
         "ice": {"state": "complete", "literal": 1, "other": 0, "masked": 0}},
    ]
    return [{"text": json.dumps(group), "truncated": False} for group in groups]


@pytest.mark.parametrize(("protocols", "launch_flag", "verdict", "transport_verdict"), [
    ({"document_protocol": "http/1.1", "echo_protocol": "h2"}, "passed", "passed", "passed"),
    ({"document_protocol": "h3"}, "unavailable", "failed", "mismatch"),
    ({"echo_protocol": "h3"}, "passed", "failed", "mismatch"),
    ({}, "passed", "partial", "unavailable"),
    ({"document_protocol": "h2", "echo_protocol": "h2"}, "mismatch", "failed", "passed"),
    ({"document_protocol": "h2", "echo_protocol": "h2"}, RAW_OBSERVATION, "partial", "passed"),
    ({"document_protocol": RAW_OBSERVATION}, "passed", "failed", None),
])
def test_transport_report_distinguishes_unexpected_protocols_from_missing_evidence(
    protocols, launch_flag, verdict, transport_verdict,
):
    rows = observation_rows()
    rendering = json.loads(rows[-1]["text"])
    rendering.pop("document_protocol")
    rendering.pop("echo_protocol")
    rows[-1]["text"] = json.dumps({**rendering, **protocols})
    result = BrowserResult("completed", 1, None, {"extractions": rows, "quic_launch_flag": launch_flag}, True)
    report = diagnostic_report(result, adapter="playwright", retain_observations=False)
    assert report["status"] == verdict
    assert RAW_OBSERVATION not in json.dumps(report)
    if transport_verdict is None:
        assert report["reason"] == "invalid_observations"
    else:
        assert report["checks"]["fixture_http_transport"] == transport_verdict


@pytest.mark.parametrize("case", [
    "browser-checks", "preset-checks", "preflight-blocked", "mismatch", "invalid", "transport", "post-load-timeout",
])
async def test_managed_owned_diagnostic_stores_sanitized_evidence_and_releases_lease(
    db, proxy_runtime, monkeypatch, tmp_path, case,
):
    monkeypatch.setenv("ABRL_DIAGNOSTIC_URL", ORIGIN + DIAGNOSTIC_PATH)
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "work")
    blocked = case == "preflight-blocked"
    reason = "country_mismatch" if blocked else None
    preflight = AsyncMock(return_value={
        "status": "blocked" if blocked else "passed", "blocked": blocked, "reason": reason,
        "checks": {"sticky": {"status": "passed"}, "timezone": {"confirmed": True, "observed": "UTC"}},
        "timings_ms": {"total": 5},
    })
    rows = observation_rows()
    if case in {"browser-checks", "post-load-timeout"}:
        rows[-1]["text"] = json.dumps({
            **json.loads(rows[-1]["text"]),
            "capabilities": {
                "audio_context": "available", "canvas_2d": "unsupported", "webgl": "unavailable",
                "network_information": RAW_OBSERVATION, RAW_OBSERVATION: "available",
            },
            "connection_timings_ms": {
                "document": {"status": "observed", "duration": 0, "request_to_first_byte": 4, "peer": RAW_OBSERVATION},
                "echo": {"status": "observed", "duration": RAW_OBSERVATION, "request_to_first_byte": -1, "connect": True},
                RAW_OBSERVATION: {"status": "observed", "duration": 42},
            },
        })
    elif case == "mismatch":
        rows[0]["text"] = json.dumps({**json.loads(rows[0]["text"]), "server_ua": "Different browser"})
        rows[2]["text"] = json.dumps({**json.loads(rows[2]["text"]), "accept_language": "fr"})
    elif case == "invalid":
        rows[0] = None
    elif case == "preset-checks":
        rows[2]["text"] = json.dumps({
            **json.loads(rows[2]["text"]), "timezone": "America/New_York", "offset_minutes": -300,
        })
        rows[3]["text"] = json.dumps({
            **json.loads(rows[3]["text"]), "inner": [390, 844], "screen": [390, 844], "dpr": 3,
        })
    metadata = {
        "runtime_stopped": True, "actual_user_agent": RAW_OBSERVATION, "extractions": rows,
        "quic_launch_flag": "passed",
        "main_origin_subresource_failures": {"image": {"http_4xx": 2, RAW_OBSERVATION: 1}, RAW_OBSERVATION: 3},
        "vendor_profile_id": "private-profile-id", "cdp_endpoint": "ws://localhost/private-debug-capability",
    }
    result = BrowserResult("completed", 5, None, metadata, True)
    if case == "transport":
        metadata.update(
            causal_failure={"phase": "workflow", "reason": "navigation_failed", "elapsed_ms": 5},
            transport_summary={"attempts": 1, "successes": 0, "failures": 1},
            transport_events=[{"kind": "transport", "reason": "proxy_tunnel_timeout"}],
        )
        result = BrowserResult("failed", 5, "private transport failure 198.51.100.25", metadata, True)
    elif case == "post-load-timeout":
        metadata["causal_failure"] = {"phase": "workflow", "reason": "navigation_timeout", "elapsed_ms": 24}
        result = BrowserResult("failed", 24, "private navigation failure 198.51.100.25", metadata, True)
    execute = AsyncMock(return_value=result)
    check_ip = AsyncMock(return_value="198.51.100.25")
    monkeypatch.setattr(session_runner, "run_decodo_preflight", preflight)
    monkeypatch.setattr(session_runner, "execute_workflow", execute)
    monkeypatch.setattr(session_runner, "check_proxy_ip", check_ip)

    with client(production=True) as api:
        def register(path, data):
            response = api.post(f"/api/v1/{path}", json=data)
            assert response.status_code == 201, response.text
            return response.json()["id"]

        target_id = register("targets", {"name": "Owned fixture", "origin": ORIGIN})
        navigate = {"action": "navigate", "url": DIAGNOSTIC_PATH}
        if case == "post-load-timeout":
            navigate["timeout_ms"] = 4500
        workflow_id = register("workflows", {
            "name": "Owned diagnostics", "steps": [navigate], "initial_navigation_timeout_ms": 18_000,
        })
        register("proxy-inventory", {
            "name": "Fixture Decodo", "provider": "decodo", "username_ref": "DECODO_USER",
            "password_ref": "DECODO_PASSWORD", "target_origin": ORIGIN,
        })
        policy = register("proxy-policies", {
            "name": "Fixture policy", "provider": "decodo", "provider_resolved_ack": True,
            "target_origin": ORIGIN, "initial_navigation_timeout_ms": 24_000,
        })
        batch = {
            "target_id": target_id, "workflow_id": workflow_id, "proxy_policy_id": policy,
            "count": 1, "idempotency_key": "owned-diag-attempt", "capture_javascript": True, "artifact_policy": "all",
        }
        if case == "preset-checks":
            batch.update(seed=42, batch_presets={"mode": "fixed", "presets": ["compact_390"]})
        register("batches", batch)
        with db.begin() as connection:
            session_runner.admission_tick(connection)
        work = session_runner.claim("standard")
        assert work is not None and work.proxy["provider"] == "decodo"
        await session_runner.execute(work)

        response = api.get(f"/api/v1/sessions/{work.id}")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert RAW_OBSERVATION not in response.text and "198.51.100.25" not in response.text
        assert "private-profile-id" not in response.text and "private-debug-capability" not in response.text
        detail = response.json()
        assert detail["observed_ip"] is None and detail["proxy_check_ip"] is None
        report = detail["metadata"]["browser_diagnostic"]
        assert report["attempt"] == detail["attempt_number"] == detail["metadata"]["proxy_preflight"]["attempt"]
        assert report["status"] == ("passed" if case in {"browser-checks", "preset-checks"} else "failed")
        assert not {
            "extractions", "actual_user_agent", "javascript_capture", "vendor_profile_id", "cdp_endpoint",
        } & detail["metadata"].keys()
        assert "observations" not in report

        timeline = api.get(f"/api/v1/sessions/{work.id}/events")
        assert timeline.status_code == 200
        assert "198.51.100.25" not in timeline.text and "test+secret" not in timeline.text
        event = next(item for item in timeline.json()["items"] if item["kind"] == "proxy_preflight")
        assert event["detail"]["attempt"] == detail["attempt_number"]
        if blocked:
            execute.assert_not_awaited()
            assert "quic_launch_flag" not in report
            assert detail["metadata"]["step_failure"]["reason"] == reason
        else:
            options = execute.await_args.kwargs
            assert options["artifact_policy"] == "none"
            assert options["recording_directory"] is None and options["javascript_capture"] is None
            assert options["steps"][0] == navigate
            assert options["workflow"]["initial_navigation_timeout_ms"] == 18_000
            assert options["proxy_navigation_timeout_ms"] == 24_000
        if case == "preset-checks":
            assert execute.await_args.kwargs["batch_preset"] == detail["batch_preset"]
            assert report["expected_timezone_source"] == "batch_preset"
            assert all(report["checks"][check] == "passed" for check in (
                "expected_language", "timezone", "timezone_offset", "device_pixel_ratio",
            ))
        if case in {"browser-checks", "post-load-timeout"}:
            assert report["observation_status"] == "complete"
            assert report["checks"]["ice"] == "passed" and report["checks"]["timezone"] == "passed"
            assert report["checks"]["fixture_http_transport"] == report["checks"]["quic_launch_flag"] == "passed"
            assert report["fixture_http_protocols"] == {"document": "h2", "echo": "h2"}
            assert report["capabilities"] == {
                "audio_context": "available", "canvas_2d": "unsupported", "webgl": "unavailable",
            }
            assert report["connection_timings_ms"] == {
                "document": {"status": "observed", "duration": 0, "request_to_first_byte": 4, "connect": None},
                "echo": {"status": "unavailable", "duration": None, "request_to_first_byte": None, "connect": None},
            }
            observed = report["recorded_observations"]
            assert observed["request_header_presence"] == {
                "user_agent": True, "accept_language": True, "client_hints_brands": False,
                "client_hints_full_versions": False, "client_hints_mobile": False,
                "client_hints_platform": False, "client_hints_platform_version": False,
            }
            assert observed["navigator_presence"]["user_agent"] is True
            assert observed["environment"] == {"timezone_present": True, "timezone_offset_minutes": 0}
            assert observed["rendering"] == {
                "viewport_px": [640, 480], "screen_px": [640, 480], "webgl_available": False,
            }
            assert report["main_origin_subresource_failures"] == {"image": {"http_4xx": 2}}
            assert detail["metadata"]["main_origin_subresource_failures"] == report["main_origin_subresource_failures"]
            if case == "post-load-timeout":
                assert report["reason"] == "navigation_timeout" and detail["status"] == "failed"
                exported = api.get(f"/api/v1/sessions/{work.id}/diagnostic-bundle")
                assert exported.status_code == 200
                fixture = exported.json()["session"]["browser_diagnostics"]
                assert fixture["fixture_status"] == "failed"
                assert fixture["fixture_observation_status"] == "complete"
                assert fixture["request_headers_vs_navigator"]["user_agent"] == "passed"
                assert fixture["environment_checks"]["viewport"] == "passed"
                assert fixture["unsupported_capabilities"] == ["canvas_2d"]
                assert fixture["recorded_observations"] == observed
                assert fixture["minimum_clock_skew_ms"] == 0
                assert RAW_OBSERVATION not in exported.text and "198.51.100.25" not in exported.text
        elif case == "mismatch":
            assert report["reason"] == "check_mismatch" and report["failed_check"] == "user_agent"
            assert report["checks"]["locale"] == "mismatch"
            assert detail["status"] == detail["workflow_outcome"] == "completed"
        elif case == "invalid":
            assert report["reason"] == "invalid_observations" and "checks" not in report
            assert report["observation_status"] == "unavailable"
        elif case == "transport":
            failure = detail["metadata"]["step_failure"]
            assert report["reason"] == failure["reason"] == "proxy_tunnel_timeout"
            assert failure["phase"] == "proxy"
        preflight.assert_awaited_once()
        check_ip.assert_not_awaited()

    with db() as connection:
        session = connection.get(BrowserSession, work.id)
        assert "198.51.100.25" not in json.dumps(session.metadata_json)
        assert connection.scalars(select(SessionAttempt)).one().proxy_check_ip is None
        assert connection.scalar(select(SessionArtifact.id)) is None
        assert connection.scalars(select(ProxyLease)).one().status == "released"
        assert connection.scalars(select(ProxyInventory)).one().active_leases == 0
