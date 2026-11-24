import asyncio
import json
import socket

import pytest
import uvicorn
from test_browser_diagnostics import chromium as chromium

from abrl import browser, browser_diagnostics, diagnostic_compare, network
from abrl.diagnostic_fixture import create_app


def timeline(*marks):
    return {
        "version": 1,
        "events": [{"event": name, "elapsed_ms": elapsed} for name, elapsed in marks],
    }


def test_compares_standalone_report_and_session_details_evidence():
    baseline = {
        "status": "passed",
        "observation_status": "complete",
        "elapsed_ms": 60,
        "workflow_elapsed_ms": 20,
        "http_status_counts": {"main_frame": {"200": 1}, "subresource": {}},
        "main_origin_subresource_failures": {},
        "transport_summary": {"attempts": 1, "successes": 1, "failures": 0, "latency_ms": 11},
        "preflight": {
            "status": "passed",
            "checks": {
                "country": {"status": "passed", "observed": "US", "confirmed": True},
                "asn": {"status": "passed", "observed": 64501, "confirmed": True},
                "sticky": {"status": "passed"},
                "latency": {"status": "observed", "source": "target_tls", "tunnel_ms": 12.5},
            },
            "timings_ms": {"total": 30, "target_tls": [{"gateway_tls_connect": 10, "target_dns": 2}]},
        },
        "fixture_http_protocols": {"document": "h2", "echo": "h2"},
        "checks": {"browser_clock": "passed", "ice": "inconclusive"},
        "capabilities": {"audio_context": "available", "webgl": "unavailable"},
        "recorded_observations": {"environment": {"timezone_offset_minutes": -480}},
        "connection_timings_ms": {"document": {"status": "observed", "duration": 19, "connect": 8}},
        "lifecycle_timeline": timeline(("preflight_completed", 30), ("navigation_started", 40), ("page_loaded", 60)),
    }
    candidate = {
        "status": "blocked", "epoch": 4, "workflow_outcome": "blocked", "elapsed_ms": 65,
        "metadata": {
            "environment_epoch": 4,
            "causal_failure": {"reason": "site_challenge"},
            "workflow_elapsed_ms": 24,
            "http_status_counts": {"main_frame": {"403": 1}, "subresource": {"404": 1}},
            "main_origin_subresource_failures": {"image": {"http_4xx": 1}},
            "transport_summary": {"attempts": 3, "successes": 2, "failures": 1, "latency_ms": 8.5},
            "proxy_preflight": {
                "epoch": 4,
                "status": "partial",
                "checks": {
                    "country": {"status": "passed", "observed": "US", "confirmed": True},
                    "asn": {"status": "unavailable", "observed": 64502, "confirmed": False},
                    "sticky": {"status": "not_applicable"},
                    "latency": {"status": "observed", "source": "lookup", "tunnel_ms": 13},
                },
                "timings_ms": {"total": 36, "target_tls": [
                    {"gateway_tls_connect": 20, "target_dns": 1, "retry_backoff": 5},
                    {"gateway_tls_connect": 8},
                ]},
            },
            "browser_diagnostic": {
                "epoch": 4, "status": "failed", "fixture_http_protocols": {"document": "http/1.1"},
                "observation_status": "unavailable",
                "capabilities": {"audio_context": "unsupported", "webgl": "available"},
                "connection_timings_ms": {"document": {"status": "observed", "duration": 14, "connect": 0}},
                "recorded_observations": {"environment": {"timezone_offset_minutes": 60}},
                "checks": {"browser_clock": "passed", "ice": "not_configured"},
            },
            "lifecycle_timeline": timeline(("navigation_started", 50), ("page_loaded", 74)),
        },
    }

    comparison = diagnostic_compare.compare_runs(baseline, candidate)

    assert comparison["outcomes"]["failure_reason"] == {
        "baseline": None, "candidate": "site_challenge", "comparison": "unknown",
    }
    assert comparison["exit_lookup"] == {
        "asn": {"baseline": 64501, "candidate": None, "comparison": "unknown"},
        "country": {"baseline": "US", "candidate": "US", "comparison": "unchanged"},
        "sticky_check": {"baseline": "passed", "candidate": "not_applicable", "comparison": "changed"},
    }
    assert comparison["protocols"]["document"] == {
        "baseline": "h2", "candidate": "http/1.1", "comparison": "changed",
    }
    assert comparison["protocols"]["echo"]["candidate"] is None
    assert comparison["lifecycle"]["navigation_deltas_ms"]["page_loaded"] == {
        "baseline": 20, "candidate": 24, "comparison": "changed", "delta_ms": 4,
    }
    assert comparison["lifecycle"]["navigation_deltas_ms"]["preflight_completed"]["baseline"] == -10
    assert comparison["timings_ms"]["preflight_total"]["delta_ms"] == 6
    assert comparison["timings_ms"]["workflow_elapsed"]["delta_ms"] == 4
    assert comparison["timings_ms"]["preflight_target_tunnel"]["delta_ms"] is None
    assert comparison["timings_ms"]["preflight_lookup_tunnel"]["delta_ms"] is None
    assert comparison["timings_ms"]["transport_tunnel_mean"]["delta_ms"] == -2.5
    probes = comparison["preflight_probe_timings_ms"]["target_tls"]
    assert probes[0]["gateway_tls_connect"]["delta_ms"] == 10
    assert probes[0]["target_dns"]["delta_ms"] == -1
    assert probes[0]["retry_backoff"]["candidate"] == 5
    assert probes[1]["gateway_tls_connect"]["comparison"] == "unknown"
    assert comparison["http_status_counts"]["main_frame"]["403"] == {
        "baseline": 0, "candidate": 1, "comparison": "changed", "delta_count": 1,
    }
    assert comparison["main_origin_subresource_failures"]["image"]["http_4xx"]["delta_count"] == 1
    assert comparison["tunnel_counts"]["failures"]["delta_count"] == 1
    assert comparison["outcomes"]["observation"]["comparison"] == "changed"
    assert comparison["recorded_observations"]["environment"]["timezone_offset_minutes"] == {
        "baseline": -480, "candidate": 60, "comparison": "changed",
    }
    assert comparison["connection_timings_ms"]["document"]["duration"]["delta_ms"] == -5
    assert comparison["connection_timings_ms"]["document"]["connect"]["candidate"] == 0
    assert comparison["capabilities"]["audio_context"]["candidate"] == "unsupported"
    assert comparison["diagnostic_checks"]["ice"]["comparison"] == "changed"


def test_discards_stale_attempt_evidence_and_preserves_old_records():
    stale = {
        "status": "queued", "epoch": 4, "workflow_outcome": "completed", "elapsed_ms": 40,
        "metadata": {
            "environment_epoch": 3,
            "causal_failure": {"reason": "site_challenge"},
            "browser_diagnostic": {"epoch": 3, "status": "passed", "fixture_http_protocols": {"document": "h3"}},
            "http_status_counts": {"main_frame": {"403": 1}, "subresource": {}},
            "lifecycle_timeline": timeline(("navigation_started", 2)),
        },
    }
    old = {"status": "completed", "workflow_outcome": "completed", "elapsed_ms": 22, "metadata": {}}

    comparison = diagnostic_compare.compare_runs(stale, old)

    assert comparison["outcomes"]["session"]["baseline"] == "queued"
    assert comparison["outcomes"]["workflow"] == {"baseline": None, "candidate": "completed", "comparison": "unknown"}
    assert comparison["outcomes"]["failure_reason"]["baseline"] is None
    assert comparison["protocols"]["document"]["baseline"] is None
    assert comparison["timings_ms"]["browser_elapsed"]["delta_ms"] is None
    assert comparison["http_status_counts"] == {}
    assert comparison["lifecycle"] == {"baseline": None, "candidate": None, "navigation_deltas_ms": {}}


def test_missing_counters_and_previous_diagnostics_are_unknown():
    candidate = {"status": "completed", "epoch": 2, "attempt_number": 3, "metadata": {
        "environment_epoch": 2,
        "browser_diagnostic": {"epoch": 2, "attempt": 2, "status": "passed"},
        "proxy_preflight": {"epoch": 2, "attempt": 2, "status": "passed"},
        "http_status_counts": {"main_frame": {"200": 1}, "subresource": {}},
        "main_origin_subresource_failures": {"fetch": {"network_error": 1}},
    }}

    comparison = diagnostic_compare.compare_runs({}, candidate)

    assert comparison["outcomes"]["session"]["candidate"] == "completed"
    assert comparison["outcomes"]["diagnostic"]["candidate"] is None
    assert comparison["outcomes"]["preflight"]["candidate"] is None
    for count in (
        comparison["http_status_counts"]["main_frame"]["200"],
        comparison["main_origin_subresource_failures"]["fetch"]["network_error"],
    ):
        assert count == {"baseline": None, "candidate": 1, "comparison": "unknown", "delta_count": None}


def test_projection_excludes_sensitive_and_invalid_values_and_keys():
    private_ip = "198.51.100.42"
    credential = "private-proxy-user-password"
    raw_ice = f"candidate:1 1 udp 123 {private_ip} 1234 typ host"
    report = {
        "status": "partial",
        "reason": credential,
        "id": "private-session-id",
        "preflight": {
            "checks": {
                "country": {"status": "passed", "confirmed": True, "observed": private_ip},
                "asn": {"status": "passed", "confirmed": True, "observed": True},
                "exit_ip": {"observed": private_ip},
            },
            "timings_ms": {
                "total": float("inf"), credential: 20,
                "lookup": [{"gateway_dns": 3, "origin_tls": credential, credential: 20}],
            },
        },
        "fixture_http_protocols": {"document": credential, "echo": "unavailable"},
        "checks": {"locale": credential, "ice": "timeout", credential: "observed"},
        "capabilities": {"canvas_2d": credential, "webgl": "unsupported", credential: "available"},
        "connection_timings_ms": {
            "echo": {"status": "observed", "duration": -1, "request_to_first_byte": True, "connect": credential}
        },
        "lifecycle_timeline": {
            "version": 1,
            "events": [
                {"event": "browser_launched", "elapsed_ms": 7, "url": "https://private-origin.example/", "cookie": credential},
                {"event": credential, "elapsed_ms": 20},
                {"event": "navigation_started", "elapsed_ms": False, "navigation_delta_ms": raw_ice},
            ],
        },
        "observations": {"candidate": raw_ice, "cookie": "private-cookie", "proxy_username": credential},
        "http_status_counts": {"main_frame": {"200": 1, private_ip: 1}, "subresource": {"503": -1}},
        "main_origin_subresource_failures": {"image": {"http_5xx": 1, credential: 2}, raw_ice: {"blocked": 3}},
        "transport_summary": {"attempts": True, "latency_ms": credential, "proxy-authorization": credential},
        "recorded_observations": {
            "environment": {"timezone_present": True, "timezone_offset_minutes": private_ip},
            "rendering": {"viewport_px": [private_ip, 720], "screen_px": [1980, 1080], "token": credential},
            credential: {"ice": raw_ice},
        },
    }

    comparison = diagnostic_compare.compare_runs({}, report)
    output = json.dumps(comparison, allow_nan=False)
    assert all(value not in output for value in (
        private_ip, credential, raw_ice, "private-cookie", "private-session-id", "private-origin.example", "proxy_username",
    ))
    assert comparison["exit_lookup"]["country"]["candidate"] is None
    assert comparison["exit_lookup"]["asn"]["candidate"] is None
    assert comparison["protocols"]["echo"]["candidate"] == "unavailable"
    assert comparison["capabilities"]["webgl"]["candidate"] == "unsupported"
    assert comparison["http_status_counts"]["subresource"] == {}
    assert comparison["tunnel_counts"]["attempts"]["candidate"] is None
    assert comparison["recorded_observations"]["rendering"]["screen_px"]["candidate"] == [1980, 1080]
    assert comparison["connection_timings_ms"]["echo"]["status"]["candidate"] == "unavailable"
    assert comparison["lifecycle"]["candidate"] == {
        "version": 1,
        "events": [{"event": "browser_launched", "elapsed_ms": 7, "navigation_delta_ms": None}],
    }


def test_cli_reads_json_records_and_prints_only_projected_comparison(tmp_path, capsys):
    baseline = tmp_path / "private-baseline.json"
    baseline.write_text(json.dumps({"status": "passed", "id": "private-id"}))
    candidate = tmp_path / "private-candidate.json"
    candidate.write_text(json.dumps({"status": "blocked", "metadata": {"causal_failure": {"reason": "site_denial"}}}))

    assert diagnostic_compare.main([str(baseline), str(candidate)]) == 0

    stdout, stderr = capsys.readouterr()
    assert stderr == ""
    assert "private-" not in stdout
    assert json.loads(stdout)["outcomes"]["failure_reason"]["candidate"] == "site_denial"


@pytest.mark.parametrize("application", ["context", "viewport_only"])
def test_cli_compares_a_paired_bundle_and_redacts_extra_fields(tmp_path, capsys, application):
    path = tmp_path / "private-bundle.json"
    baseline = {
        "session_id": "private-baseline",
        "batch_preset": {"catalog_version": 1, "id": "desktop_1280", "locale": "en-US", "mode": "fixed", "seed": 7},
        "lifecycle_outcome": {
            "status": "completed", "elapsed_ms": 1300, "workflow_elapsed_ms": 250,
            "initial_navigation_timeout": {"load_timeout_ms": 5000, "load_timeout_source": "direct_default"},
            "http_status_counts": {"main_frame": {"200": 1}, "subresource": {}},
        },
        "proxy_network": {"exit_lookup": {"country": "US", "asn": 64500}, "negotiated_http": {"document": "h2"}},
        "browser_diagnostics": {"fixture_status": "passed", "capabilities": {"webgl": "available"}},
    }
    selected = {
        "batch_preset": {
            "catalog_version": 1, "id": "compact_390", "mode": "seeded", "seed": 51,
            "application": application, "locale": "en-GB" if application == "context" else None,
            "timezone": "private-state", "headers": "private-headers",
        },
        "lifecycle_outcome": {
            "status": "failed", "elapsed_ms": 1500, "workflow_elapsed_ms": 300,
            "initial_navigation_timeout": {
                "load_timeout_ms": 20_000, "load_timeout_source": "proxy_policy", "credential": "private-auth",
            },
            "http_status_counts": {"main_frame": {"302": 1, "200": 1}, "subresource": {}},
            "first_causal_failure": {
                "reason": "navigation_timeout", "http_status": 200, "url": "https://private-origin.example",
            },
        },
        "proxy_network": {"exit_lookup": {"country": "198.51.100.1", "asn": "private-token"}},
        "browser_diagnostics": {"fixture_status": "failed", "capabilities": {"webgl": "unsupported"}},
    }
    path.write_text(json.dumps({
        "schema": "abrl.diagnostic_bundle", "version": 1, "baseline": baseline, "session": selected,
    }))
    assert diagnostic_compare.main([str(path)]) == 0
    output, errors = capsys.readouterr()
    report = json.loads(output)
    assert errors == "" and "private-" not in output and "198.51.100.1" not in output
    assert report["timings_ms"]["browser_elapsed"]["delta_ms"] == 200
    assert report["timings_ms"]["workflow_elapsed"]["delta_ms"] == 50
    assert report["http_status_counts"]["main_frame"]["302"]["delta_count"] == 1
    assert report["exit_lookup"]["country"] == {"baseline": "US", "candidate": None, "comparison": "unknown"}
    assert report["outcomes"]["failure_reason"]["candidate"] == "navigation_timeout"
    assert report["first_causal_failure"]["candidate"]["http_status"] == 200
    assert report["initial_navigation_timeout"] == {
        "baseline": {"load_timeout_ms": 5000, "load_timeout_source": "direct_default"},
        "candidate": {"load_timeout_ms": 20_000, "load_timeout_source": "proxy_policy"},
        "comparison": "changed",
    }
    assert report["capabilities"]["webgl"]["comparison"] == "changed"
    assert report["batch_preset"]["id"] == {
        "baseline": "desktop_1280", "candidate": "compact_390", "comparison": "changed",
    }
    assert report["batch_preset"]["application"]["candidate"] == application
    if application == "viewport_only":
        assert report["batch_preset"]["timezone"] == {
            "baseline": "America/New_York", "candidate": None, "comparison": "unknown",
        }
        assert report["batch_preset"]["device_pixel_ratio"]["candidate"] is None
    else:
        assert report["batch_preset"]["timezone"]["candidate"] == "Europe/London"


def test_comparison_distinguishes_first_tunnel_failure_from_slow_navigation():
    navigation_wait = {
        "stage": "initial_dom_content_loaded", "deadline": "step",
        "load_timeout_ms": 5000, "load_timeout_source": "direct_default",
    }
    broker_wait = {"stage": "proxy_connect", "stage_elapsed_ms": 420}

    def record(successes):
        return {"status": "failed", "metadata": {
            "causal_failure": {"reason": "navigation_timeout", "navigation_wait": navigation_wait},
            "transport_summary": {"attempts": 1, "successes": successes},
            "transport_events": [{
                "reason": "proxy_tunnel_timeout", "tunnel_wait": {**broker_wait, "token": "private-auth-token"},
            }],
        }}

    comparison = diagnostic_compare.compare_runs(record(1), record(0))
    assert comparison["outcomes"]["failure_reason"] == {
        "baseline": "navigation_timeout", "candidate": "proxy_tunnel_timeout", "comparison": "changed",
    }
    assert comparison["navigation_wait"] == {"baseline": navigation_wait, "candidate": navigation_wait}
    assert comparison["tunnel_wait"] == {"baseline": None, "candidate": broker_wait}
    standalone = {"status": "failed", "first_causal_failure": {"reason": "proxy_tunnel_timeout", "tunnel_wait": broker_wait}}
    assert diagnostic_compare.compare_runs(record(1), standalone)["tunnel_wait"]["candidate"] == broker_wait
    assert "private-auth-token" not in json.dumps(comparison)

    standalone["first_causal_failure"]["navigation_wait"] = {
        **navigation_wait, "load_timeout_ms": 20_000, "load_timeout_source": "proxy_policy",
    }
    comparison = diagnostic_compare.compare_runs(record(1), standalone)
    assert comparison["navigation_wait"] == {
        "baseline": navigation_wait, "candidate": standalone["first_causal_failure"]["navigation_wait"],
    }


@pytest.mark.parametrize("content", ["private-password=not-json", "[]", "[" * 2000])
def test_cli_rejects_invalid_exports_without_echoing_paths_or_content(tmp_path, capsys, content):
    path = tmp_path / "private-source.json"
    path.write_text(content)

    with pytest.raises(SystemExit) as error:
        diagnostic_compare.main([str(path), str(path)])

    stdout, stderr = capsys.readouterr()
    assert error.value.code == 2
    assert stdout == ""
    assert "readable JSON objects" in stderr
    assert "private-" not in stderr


def test_cli_rejects_oversized_input(tmp_path, capsys, monkeypatch):
    path = tmp_path / "report.json"
    path.write_text('{"status":"passed"}')
    monkeypatch.setattr(diagnostic_compare, "_MAX_EXPORT_BYTES", 4)

    with pytest.raises(SystemExit) as error:
        diagnostic_compare.main([str(path), str(path)])

    assert error.value.code == 2
    assert capsys.readouterr().out == ""


@pytest.mark.integration
async def test_compares_two_real_loopback_diagnostic_runs(chromium, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        monkeypatch.setattr(network, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(network, "FIXTURE_PORT", port)
        monkeypatch.setattr(browser, "FIXTURE_ORIGIN", origin)
        monkeypatch.setattr(browser_diagnostics, "FIXTURE_ORIGIN", origin)
        server = uvicorn.Server(uvicorn.Config(create_app(), log_level="error", access_log=False))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    await asyncio.sleep(0.01)
            baseline = await browser_diagnostics.run_browser_diagnostics(origin=origin)
            candidate = await browser_diagnostics.run_browser_diagnostics(origin=origin, expected_language="zz")
        finally:
            server.should_exit = True
            await task

    comparison = diagnostic_compare.compare_runs(baseline, candidate)
    assert comparison["outcomes"]["diagnostic"]["baseline"] in {"passed", "partial"}
    assert comparison["outcomes"]["diagnostic"]["candidate"] == "failed"
    assert comparison["outcomes"]["failure_reason"]["candidate"] == "check_mismatch"
    assert comparison["diagnostic_checks"]["expected_language"] == {
        "baseline": "not_configured", "candidate": "mismatch", "comparison": "changed",
    }
    assert comparison["protocols"]["echo"] == {
        "baseline": "http/1.1", "candidate": "http/1.1", "comparison": "unchanged",
    }
    assert comparison["capabilities"]["canvas_2d"]["baseline"] == "available"
    assert comparison["timings_ms"]["browser_elapsed"]["baseline"] > 0
    assert comparison["http_status_counts"]["main_frame"]["200"]["delta_count"] == 0
    assert comparison["http_status_counts"]["subresource"]["200"]["candidate"] == 1
    assert comparison["recorded_observations"]["navigator_presence"]["user_agent"]["comparison"] == "unchanged"
    assert comparison["connection_timings_ms"]["echo"]["status"]["candidate"] == "observed"
    assert comparison["lifecycle"]["navigation_deltas_ms"]["navigation_started"]["delta_ms"] == 0
    output = json.dumps(comparison)
    assert all(value not in output for value in (
        "127.0.0.1", "/lab/diagnostics", baseline["observations"]["identity"]["browser_ua"],
    ))
