"""Project recorded network and browser evidence onto a closed export schema."""

import re

from .browser_diagnostics import (
    public_capabilities,
    public_connection_timings,
    public_recorded_observations,
)
from .browser_network_trace import TUNNEL_COUNT_FIELDS, public_transport_summary
from .lifecycle import public_duration_ms
from .network import PROXY_TRANSPORT_REASONS
from .proxy_preflight import LOOKUP_DATASET_STATUSES
from .session_metadata import public_viewport_conformance

_ADAPTERS = ("playwright", "cloakbrowser", "kameleo", "manual_chrome")
_REPORT_STATUSES = ("passed", "partial", "failed")
_PREFLIGHT_STATUSES = ("passed", "partial", "unavailable", "blocked", "cancelled")
_LOOKUP_REASONS = (
    *PROXY_TRANSPORT_REASONS,
    "authentication", "quota", "binding_unavailable", "unsupported_target", "cancelled", "preflight_timeout",
    "broker_connect_timeout", "broker_connect_failed", "proxy_connect_timeout", "proxy_connect_failed",
    "origin_tls_timeout", "origin_tls_failed", "lookup_response_timeout", "lookup_response_failed",
    "destination_unavailable", "proxy_response_too_large", "lookup_redirect", "lookup_http_error",
    "invalid_lookup_response", "invalid_exit_ip", "lookup_response_empty", "lookup_response_too_large",
    "evidence_unavailable", "unconfirmed_provider_evidence", "sources_disagree", "conflicting_classifications",
    "country_mismatch", "asn_not_allowed", "hosting_provider", "first_lookup_unavailable", "sticky_ip_changed",
    "tunnel_timing_unavailable", "latency_threshold_exceeded",
)
_CHECK_STATUSES = (
    "passed", "failed", "observed", "mismatch", "unsupported", "unavailable", "inconclusive",
    "timeout", "not_configured", "not_run", "not_applicable",
)
_NEGOTIATED_PROTOCOLS = ("http/1.0", "http/1.1", "h2", "h3")
_FIXTURE_PROTOCOLS = (*_NEGOTIATED_PROTOCOLS, "unavailable")
_CONTINUITY_MODES = ("sticky", "strict", "rotating", "fixed")
_LOCAL_EGRESS = "local_browser_proxy; host OS egress is not sandboxed"
_REMOTE_EGRESS = "remote broker; deployment network policies required"
_HEADER_COMPARISONS = (
    "user_agent", "chromium_major", "client_hints_full_versions", "user_agent_platform",
    "client_hints_platform", "client_hints_platform_version", "client_hints_mobile", "locale",
    "unexpected_proxy_headers",
)
_ENVIRONMENT_CHECKS = (
    "expected_language", "timezone", "timezone_offset", "browser_clock", "viewport",
    "device_pixel_ratio", "webgl", "browser_automation", "ice", "fixture_http_transport", "quic_launch_flag",
)
FIXTURE_CHECKS = _HEADER_COMPARISONS + _ENVIRONMENT_CHECKS
_PROBE_STAGES = (
    "target_dns", "gateway_dns", "gateway_tls_connect", "proxy_connect", "tunnel_establish", "origin_tls",
    "connection", "first_byte", "total", "retry_backoff",
)


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _choice(value: object, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _count(value: object, maximum: int = 2**53 - 1) -> int | None:
    return value if type(value) is int and 0 <= value <= maximum else None


def _current_report(value: object, epoch: int | None, attempt_number: int | None) -> dict:
    report = _object(value)
    for key, expected in (("epoch", epoch), ("attempt", attempt_number)):
        if expected is not None and key in report:
            if type(report[key]) is not int or report[key] != expected:
                return {}
    return report


def _confirmed_observation(check: object) -> object:
    check = _object(check)
    if check.get("confirmed") is True and check.get("status") in ("passed", "failed"):
        return check.get("observed")
    return None


def _exit_lookup_evidence(checks: dict, preflight_reason: str | None) -> dict:
    exit_probe = _object(checks.get("exit_ip"))
    exit_reason = _choice(exit_probe.get("reason"), _LOOKUP_REASONS)
    result = {}
    for field, key in (("country", "country"), ("asn", "asn"), ("network_classification", "network_type")):
        check = _object(checks.get(key))
        status = _choice(check.get("status"), _CHECK_STATUSES)
        reason = _choice(check.get("reason"), _LOOKUP_REASONS)
        if status == "not_run" and reason is None:
            reason = exit_reason or preflight_reason
        result[field] = {
            "status": status,
            "reason": reason,
            "dataset_status": _choice(check.get("dataset_status"), LOOKUP_DATASET_STATUSES),
        }
    return result


def public_probe_timings(preflight_timing: dict) -> dict:
    """Keep bounded phase timings in the order each probe attempt ran."""
    timings = {}
    for name in ("lookup", "sticky_lookup", "target_tls"):
        samples = preflight_timing.get(name)
        if not isinstance(samples, list):
            continue
        safe = []
        for sample in samples[:2]:
            row = _object(sample)
            safe.append({
                stage: duration for stage in _PROBE_STAGES
                if (duration := public_duration_ms(row.get(stage))) is not None
            })
        if any(safe):
            timings[name] = safe
    return timings


def bundle_network(
    metadata: dict,
    *,
    assigned_continuity: str | None = None,
    epoch: int | None = None,
    attempt_number: int | None = None,
) -> dict:
    """Return safe network observations and identify configuration-only evidence."""
    proxy = _object(metadata.get("proxy"))
    provider = _choice(proxy.get("provider"), ("decodo", "oxylabs"))
    continuity = _choice(proxy.get("continuity"), _CONTINUITY_MODES + ("none",))
    if continuity == "none":
        continuity = "rotating" if proxy.get("product") == "residential" or provider == "decodo" else None
    assigned = _choice(assigned_continuity, _CONTINUITY_MODES + ("direct",))
    preflight = _current_report(metadata.get("proxy_preflight"), epoch, attempt_number)
    checks = _object(preflight.get("checks"))
    preflight_reason = _choice(preflight.get("reason"), _LOOKUP_REASONS)
    diagnostic = _current_report(metadata.get("browser_diagnostic"), epoch, attempt_number)
    diagnostic_checks = _object(diagnostic.get("checks"))
    country = _confirmed_observation(checks.get("country"))
    asn = _count(_confirmed_observation(checks.get("asn")), 2**32 - 1) or None
    classification = _object(checks.get("network_type"))
    network_type = (
        _choice(classification.get("observed"), ("residential", "hosting"))
        if classification.get("source") == "maxmind" and classification.get("status") in ("passed", "failed")
        else None
    )
    protocols = _object(diagnostic.get("fixture_http_protocols"))
    fixture_document = _choice(protocols.get("document"), _FIXTURE_PROTOCOLS)
    echo_protocol = _choice(protocols.get("echo"), _FIXTURE_PROTOCOLS)
    observed_document = _choice(metadata.get("main_document_http_protocol"), _NEGOTIATED_PROTOCOLS)
    if fixture_document in _NEGOTIATED_PROTOCOLS:
        document_protocol, document_source = fixture_document, "diagnostic_fixture"
    elif observed_document is not None:
        document_protocol, document_source = observed_document, "initial_document_cdp"
    else:
        document_protocol, document_source = fixture_document, None
    launch_check = next((safe for value in (
        diagnostic_checks.get("quic_launch_flag"), diagnostic.get("quic_launch_flag"), metadata.get("quic_launch_flag"),
    ) if (safe := _choice(value, ("passed", "mismatch", "unavailable"))) is not None), None)
    broker_configured = metadata.get("egress_enforcement") in (_LOCAL_EGRESS, _REMOTE_EGRESS)
    provider_resolved = (
        proxy.get("dns_mode") == "provider_resolved"
        or provider is not None
        or assigned in _CONTINUITY_MODES
    )
    preflight_timing = _object(preflight.get("timings_ms"))
    probe_timings = public_probe_timings(preflight_timing)
    gateway_probes = [sample for samples in probe_timings.values() for sample in samples if "gateway_dns" in sample]
    gateway_dns_check = (
        "resolved" if any("gateway_tls_connect" in sample for sample in gateway_probes)
        else "attempted" if gateway_probes else "unmeasured"
    )
    transport = public_transport_summary(metadata.get("transport_summary")) or {}
    latency = _object(checks.get("latency"))
    latency_source = _choice(latency.get("source"), ("lookup", "target_tls"))

    return {
        "proxy": {
            "provider": provider,
            "continuity": continuity or assigned,
            "continuity_source": (
                "browser_binding" if continuity else
                "direct_attempt" if assigned == "direct" else "assigned_lease" if assigned else None
            ),
            "sticky_check": _choice(_object(checks.get("sticky")).get("status"), _CHECK_STATUSES),
        },
        "exit_lookup": {
            "country": country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else None,
            "asn": asn,
            "network_classification": network_type,
            "preflight_status": _choice(preflight.get("status"), _PREFLIGHT_STATUSES),
            "preflight_reason": preflight_reason,
            "evidence": _exit_lookup_evidence(checks, preflight_reason),
        },
        "negotiated_http": {"document": document_protocol, "document_source": document_source, "echo": echo_protocol},
        "quic": {
            "launch_check": launch_check,
            "document_http3_observed": document_protocol == "h3" if document_source is not None else None,
            "fixture_http3_observed": (
                True if "h3" in (fixture_document, echo_protocol)
                else False if all(protocol in _NEGOTIATED_PROTOCOLS for protocol in (fixture_document, echo_protocol))
                else None
            ),
        },
        "ipv6": {
            "broker_http_policy": "ipv4_only" if broker_configured else None,
            "end_to_end_observation": "unmeasured",
        },
        "dns": {
            "target_resolution_policy": (
                "provider_resolved" if provider_resolved else "local_validated_ipv4" if broker_configured else None
            ),
            "gateway_probe_check": gateway_dns_check,
            "end_to_end_resolver_observation": "unmeasured",
        },
        "timings_ms": {
            "preflight_total": public_duration_ms(preflight_timing.get("total")),
            "preflight_tunnels": probe_timings,
            "preflight_tunnel_latency": public_duration_ms(latency.get("tunnel_ms")) if latency_source else None,
            "preflight_tunnel_source": latency_source,
            "browser_tunnel_average": transport.get("latency_ms"),
        },
        "connection_timings_ms": public_connection_timings(diagnostic.get("connection_timings_ms")) or {},
        "tunnel_counts": {name: transport.get(name) for name in TUNNEL_COUNT_FIELDS},
    }


def bundle_browser(session, metadata: dict) -> dict:
    """Return recorded browser comparison verdicts without underlying fingerprint values."""
    diagnostic = _current_report(metadata.get("browser_diagnostic"), session.epoch, session.attempt_number)
    raw_checks = _object(diagnostic.get("checks"))
    checks = {
        name: status for name in FIXTURE_CHECKS
        if (status := _choice(raw_checks.get(name), _CHECK_STATUSES)) is not None
    }
    capabilities = public_capabilities(diagnostic.get("capabilities")) or {}
    return {
        "adapter": _choice(session.adapter, _ADAPTERS),
        "profile_mode": _choice(session.persistence, ("fresh", "persistent")),
        "profile_launch_state": _choice(metadata.get("profile_launch_state"), ("fresh", "initialized", "restored")),
        "epoch": _count(session.epoch),
        "attempt": _count(session.attempt_number),
        "device_mode": _choice(metadata.get("device_mode"), ("browser_default", "mobile_emulation", "responsive_viewport")),
        "viewport_conformance": public_viewport_conformance(metadata.get("viewport_conformance")),
        "fixture_status": _choice(diagnostic.get("status"), _REPORT_STATUSES),
        "fixture_observation_status": _choice(diagnostic.get("observation_status"), ("complete", "unavailable")),
        "request_headers_vs_navigator": {name: checks[name] for name in _HEADER_COMPARISONS if name in checks},
        "environment_checks": {name: checks[name] for name in _ENVIRONMENT_CHECKS if name in checks},
        "capabilities": capabilities,
        "recorded_observations": public_recorded_observations(diagnostic.get("recorded_observations")) or {},
        "mismatches": [name for name, status in checks.items() if status == "mismatch"],
        "unsupported_checks": [name for name, status in checks.items() if status == "unsupported"],
        "unsupported_capabilities": [name for name, status in capabilities.items() if status == "unsupported"],
        "minimum_clock_skew_ms": public_duration_ms(diagnostic.get("minimum_clock_skew_ms")),
    }
