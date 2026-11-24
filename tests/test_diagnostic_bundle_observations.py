import json
from types import SimpleNamespace

from abrl.diagnostic_bundle_observations import bundle_browser, bundle_network


def test_bundle_network_projects_confirmed_exit_and_measured_transport():
    metadata = {
        "egress_enforcement": "local_browser_proxy; host OS egress is not sandboxed",
        "proxy": {
            "provider": "decodo", "product": "residential", "continuity": "sticky",
            "country": "CA", "host": "private-proxy.example", "session_key": "private-session-key",
        },
        "proxy_preflight": {
            "epoch": 2, "attempt": 1, "status": "partial",
            "checks": {
                "country": {"status": "failed", "confirmed": True, "observed": "US", "expected": "CA"},
                "asn": {"status": "passed", "confirmed": True, "observed": 64496},
                "network_type": {"status": "passed", "observed": "residential", "source": "maxmind"},
                "sticky": {"status": "passed", "ip": "198.51.100.11"},
                "latency": {"status": "passed", "source": "target_tls", "tunnel_ms": 17.125},
            },
            "timings_ms": {
                "total": 123,
                "lookup": [{"gateway_dns": 5, "gateway_tls_connect": 11, "origin_tls": 37, "private-sample": 700}],
                "sticky_lookup": [{"total": 41}],
                "target_tls": [
                    {"tunnel_establish": 17.125}, {"tunnel_establish": False, "retry_backoff": 200}, {"total": 8},
                ],
            },
        },
        "browser_diagnostic": {
            "epoch": 2, "attempt": 1,
            "fixture_http_protocols": {"document": "h2", "echo": "h3", "private-protocol": "sensitive"},
            "checks": {"quic_launch_flag": "passed"},
            "connection_timings_ms": {
                "document": {"status": "observed", "duration": 54, "connect": 12, "cookie": "private-cookie"},
                "echo": {"status": "unsupported", "duration": 99},
            },
        },
        "transport_summary": {"attempts": 3, "successes": 2, "failures": 1, "latency_ms": 24.25},
        "transport_events": [{"upstream_authority": "private-gateway.example:443"}],
    }

    report = bundle_network(metadata, epoch=2, attempt_number=1)

    assert report["proxy"] == {
        "provider": "decodo", "continuity": "sticky", "continuity_source": "browser_binding", "sticky_check": "passed",
    }
    assert report["exit_lookup"] == {
        "country": "US", "asn": 64496, "network_classification": "residential", "preflight_status": "partial",
        "preflight_reason": None,
        "evidence": {
            "country": {"status": "failed", "reason": None, "dataset_status": None},
            "asn": {"status": "passed", "reason": None, "dataset_status": None},
            "network_classification": {"status": "passed", "reason": None, "dataset_status": None},
        },
    }
    assert report["negotiated_http"] == {"document": "h2", "document_source": "diagnostic_fixture", "echo": "h3"}
    assert report["quic"] == {"launch_check": "passed", "document_http3_observed": False, "fixture_http3_observed": True}
    assert report["ipv6"] == {"broker_http_policy": "ipv4_only", "end_to_end_observation": "unmeasured"}
    assert report["dns"]["target_resolution_policy"] == "provider_resolved"
    assert report["dns"]["gateway_probe_check"] == "resolved"
    assert report["timings_ms"] == {
        "preflight_total": 123,
        "preflight_tunnels": {
            "lookup": [{"gateway_dns": 5, "gateway_tls_connect": 11, "origin_tls": 37}],
            "sticky_lookup": [{"total": 41}], "target_tls": [{"tunnel_establish": 17.125}, {"retry_backoff": 200}],
        },
        "preflight_tunnel_latency": 17.125, "preflight_tunnel_source": "target_tls", "browser_tunnel_average": 24.25,
    }
    assert report["connection_timings_ms"] == {
        "document": {"status": "observed", "duration": 54, "connect": 12, "request_to_first_byte": None},
        "echo": {"status": "unsupported", "duration": None, "connect": None, "request_to_first_byte": None},
    }
    assert report["tunnel_counts"] == {"attempts": 3, "successes": 2, "failures": 1, "capacity_rejections": None}
    packed = json.dumps(report)
    assert all(value not in packed for value in ("198.51.100.11", "private-", '"CA"', "upstream_authority"))


def test_bundle_network_marks_legacy_missing_and_stale_observations():
    stale = bundle_network({
        "egress_enforcement": "local_browser_proxy; host OS egress is not sandboxed",
        "proxy_preflight": {"epoch": 1, "status": "passed", "checks": {
            "country": {"status": "passed", "confirmed": True, "observed": "GB"},
        }},
        "browser_diagnostic": {"epoch": 2, "attempt": 1, "fixture_http_protocols": {"document": "h2"}},
        "transport_summary": {"latency_ms": float("inf"), "attempts": True, "successes": -1},
    }, epoch=2, attempt_number=2, assigned_continuity="direct")
    assert stale["exit_lookup"]["preflight_status"] is None
    assert stale["exit_lookup"]["country"] is None
    assert stale["negotiated_http"] == {"document": None, "document_source": None, "echo": None}
    assert stale["quic"] == {"launch_check": None, "document_http3_observed": None, "fixture_http3_observed": None}
    assert stale["proxy"]["continuity"] == "direct"
    assert stale["proxy"]["continuity_source"] == "direct_attempt"
    assert stale["dns"]["target_resolution_policy"] == "local_validated_ipv4"
    assert stale["dns"]["gateway_probe_check"] == "unmeasured"
    assert stale["timings_ms"]["browser_tunnel_average"] is None
    assert all(value is None for value in stale["tunnel_counts"].values())
    json.dumps(stale, allow_nan=False)


def test_bundle_network_distinguishes_unconfirmed_lookup_from_configured_lease():
    report = bundle_network({
        "proxy_preflight": {
            "epoch": 1, "attempt": 1, "status": "blocked",
            "checks": {
                "country": {"status": "unavailable", "confirmed": False, "observed": "US"},
                "asn": {"status": "passed", "confirmed": True, "observed": 2**32},
                "network_type": {"status": "passed", "source": "provider", "observed": "residential"},
                "sticky": {"status": "not_run"},
            },
            "timings_ms": {"target_tls": [{"gateway_dns": 21}]},
        },
    }, epoch=1, attempt_number=1, assigned_continuity="rotating")

    assert report["exit_lookup"] == {
        "preflight_status": "blocked", "country": None, "asn": None, "network_classification": None,
        "preflight_reason": None,
        "evidence": {
            "country": {"status": "unavailable", "reason": None, "dataset_status": None},
            "asn": {"status": "passed", "reason": None, "dataset_status": None},
            "network_classification": {"status": "passed", "reason": None, "dataset_status": None},
        },
    }
    assert report["proxy"]["continuity"] == "rotating"
    assert report["proxy"]["continuity_source"] == "assigned_lease"
    assert report["proxy"]["sticky_check"] == "not_run"
    assert report["ipv6"]["broker_http_policy"] is None
    assert report["dns"]["gateway_probe_check"] == "attempted"


def test_bundle_explains_partial_exit_lookup_without_exporting_unconfirmed_or_private_data():
    report = bundle_network({
        "proxy_preflight": {
            "status": "partial", "reason": "unconfirmed_provider_evidence",
            "checks": {
                "exit_ip": {"status": "passed", "observed": "198.51.100.19"},
                "country": {
                    "status": "unavailable", "reason": "unconfirmed_provider_evidence", "confirmed": False,
                    "observed": "CA", "dataset_status": "not_configured", "path": "/private/City.mmdb",
                },
                "asn": {
                    "status": "unavailable", "reason": "unconfirmed_provider_evidence", "confirmed": False,
                    "observed": 64496, "dataset_status": "invalid_or_incompatible",
                },
                "network_type": {
                    "status": "unavailable", "reason": "evidence_unavailable",
                    "dataset_status": "/private/Anonymous.mmdb",
                },
                "sticky": {"status": "passed"},
                "target_tls": {"status": "passed"},
            },
        },
    })

    assert report["proxy"]["sticky_check"] == "passed"
    assert report["exit_lookup"] == {
        "preflight_status": "partial", "preflight_reason": "unconfirmed_provider_evidence",
        "country": None, "asn": None, "network_classification": None,
        "evidence": {
            "country": {"status": "unavailable", "reason": "unconfirmed_provider_evidence",
                        "dataset_status": "not_configured"},
            "asn": {"status": "unavailable", "reason": "unconfirmed_provider_evidence",
                    "dataset_status": "invalid_or_incompatible"},
            "network_classification": {"status": "unavailable", "reason": "evidence_unavailable",
                                       "dataset_status": None},
        },
    }
    packed = json.dumps(report)
    assert all(marker not in packed for marker in ("198.51.100.19", '"CA"', "64496", ".mmdb", "/private/"))


def test_bundle_reports_when_exit_lookup_failed_before_local_dataset_checks():
    report = bundle_network({"proxy_preflight": {"status": "partial", "checks": {
        "exit_ip": {"status": "unavailable", "reason": "lookup_response_timeout"},
        "country": {"status": "not_run", "reason": "https://private.invalid"},
        "asn": {"status": "not_run"},
        "network_type": {"status": "not_run"},
    }}})

    for evidence in report["exit_lookup"]["evidence"].values():
        assert evidence == {"status": "not_run", "reason": "lookup_response_timeout", "dataset_status": None}
    assert "private.invalid" not in json.dumps(report)


def test_bundle_network_labels_passive_document_protocol_separately_from_fixture():
    report = bundle_network({
        "main_document_http_protocol": "h2",
        "browser_diagnostic": {"fixture_http_protocols": {"document": "unavailable"}},
    })

    assert report["negotiated_http"] == {"document": "h2", "document_source": "initial_document_cdp", "echo": None}
    assert report["quic"]["document_http3_observed"] is False
    assert report["quic"]["fixture_http3_observed"] is None
    assert bundle_network({"main_document_http_protocol": "https://198.51.100.13"})["negotiated_http"]["document"] is None


def test_bundle_browser_exports_verdicts_and_coarse_capabilities_without_values():
    session = SimpleNamespace(adapter="playwright", persistence="persistent", epoch=3, attempt_number=2)
    report = bundle_browser(session, {
        "profile_launch_state": "restored", "device_mode": "mobile_emulation",
        "viewport_conformance": {"checks": 2, "matched": 1, "actual": {"width": 913}},
        "actual_user_agent": "private-agent",
        "browser_diagnostic": {
            "epoch": 3, "attempt": 2, "status": "failed",
            "observation_status": "complete",
            "checks": {
                "user_agent": "mismatch", "locale": "passed", "unexpected_proxy_headers": "passed",
                "viewport": "mismatch", "timezone": "passed", "webgl": "unsupported", "ice": "inconclusive",
                "browser_clock": "private-clock", "authorization": "passed",
            },
            "capabilities": {"canvas_2d": "available", "webgl": "unsupported", "private-feature": "available"},
            "recorded_observations": {
                "request_header_presence": {
                    "user_agent": True, "accept_language": "private-language", "Cookie": "private-cookie",
                },
                "navigator_presence": {"user_agent": False, "languages": True, "client_hints_mobile": 1},
                "environment": {"timezone_present": True, "timezone_offset_minutes": -240, "timezone": "private-zone"},
                "rendering": {
                    "viewport_px": [320, 640], "screen_px": [True, 5000], "webgl_available": False,
                    "vendor": "private-vendor",
                },
            },
            "observations": {
                "request_headers": {"Cookie": "private-cookie", "Authorization": "private-authorization"},
                "webgl": {"renderer": "private-renderer"},
                "ice": {"candidate": "198.51.100.12"},
            },
            "minimum_clock_skew_ms": 4,
        },
    })

    assert report == {
        "adapter": "playwright", "profile_mode": "persistent", "profile_launch_state": "restored",
        "epoch": 3, "attempt": 2, "device_mode": "mobile_emulation",
        "viewport_conformance": {"checks": 2, "matched": 1}, "fixture_status": "failed",
        "fixture_observation_status": "complete",
        "request_headers_vs_navigator": {"user_agent": "mismatch", "locale": "passed", "unexpected_proxy_headers": "passed"},
        "environment_checks": {"viewport": "mismatch", "timezone": "passed", "webgl": "unsupported", "ice": "inconclusive"},
        "capabilities": {"canvas_2d": "available", "webgl": "unsupported"},
        "recorded_observations": {
            "request_header_presence": {"user_agent": True},
            "navigator_presence": {"user_agent": False, "languages": True},
            "environment": {"timezone_present": True, "timezone_offset_minutes": -240},
            "rendering": {"viewport_px": [320, 640], "webgl_available": False},
        },
        "mismatches": ["user_agent", "viewport"], "unsupported_checks": ["webgl"],
        "unsupported_capabilities": ["webgl"], "minimum_clock_skew_ms": 4,
    }
    packed = json.dumps(report)
    assert all(value not in packed for value in ("198.51.100.12", "private-", "request_headers\"", "913"))


def test_bundle_browser_requires_current_attempt_and_valid_runtime_enums():
    session = SimpleNamespace(adapter="private-adapter", persistence="private-mode", epoch=3, attempt_number=2)
    report = bundle_browser(session, {
        "profile_launch_state": "private-path", "device_mode": "private-device",
        "viewport_conformance": {"checks": 1, "matched": 2},
        "browser_diagnostic": {"epoch": 3, "attempt": True, "checks": {"locale": "passed"}},
    })
    assert report["adapter"] is None and report["profile_mode"] is None
    assert report["profile_launch_state"] is None and report["device_mode"] is None
    assert report["viewport_conformance"] is None
    assert report["request_headers_vs_navigator"] == {}
    assert report["recorded_observations"] == {}
    assert "private-" not in json.dumps(report)
