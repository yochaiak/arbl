"""Compare two local diagnostic exports using only explicitly allowed observations."""

import argparse
import json
import re
from itertools import zip_longest
from pathlib import Path

from .batch_presets import preset_details
from .browser_diagnostics import (
    public_capabilities,
    public_connection_timings,
    public_recorded_observations,
)
from .browser_network_trace import (
    TUNNEL_COUNT_FIELDS,
    public_http_status_counts,
    public_transport_checks,
    public_transport_summary,
)
from .diagnostic_bundle_lifecycle import public_lifecycle_failure
from .diagnostic_bundle_observations import FIXTURE_CHECKS, public_probe_timings
from .lifecycle import (
    public_duration_ms,
    public_initial_navigation_timeout,
    public_lifecycle_timeline,
    public_navigation_wait,
    public_tunnel_wait,
)
from .network import first_proxy_tunnel_failure

_MAX_EXPORT_BYTES = 16 * 1024 * 1024
_BUNDLE_SCHEMA = "abrl.diagnostic_bundle"
_SESSION_STATES = (
    "queued", "starting", "preparing", "ready", "running", "awaiting_operator", "stopping",
    "completed", "failed", "blocked", "cancelled", "needs_reconciliation",
)
_WORKFLOW_STATES = ("completed", "failed", "blocked", "cancelled", "not_started")
_REPORT_STATES = ("passed", "partial", "failed")
_PREFLIGHT_STATES = ("passed", "partial", "unavailable", "blocked", "cancelled")
_CHECK_STATES = (
    "passed", "failed", "mismatch", "observed", "unsupported", "unavailable", "timeout", "inconclusive",
    "not_configured", "not_run", "not_applicable",
)
_PROTOCOLS = ("http/1.0", "http/1.1", "h2", "h3", "unavailable")


def _object(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _choice(value: object, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def _current_evidence(value: object, **markers: object) -> dict | None:
    evidence = _object(value)
    for marker, expected in markers.items():
        if type(expected) is int and marker in evidence:
            given = evidence[marker]
            if type(given) is not int or given != expected:
                return None
    return evidence


def _confirmed_lookup(value: object) -> object:
    check = _object(value)
    # A confirmed policy mismatch still contains independently corroborated lookup evidence.
    if check.get("confirmed") is True and check.get("status") in ("passed", "failed"):
        return check.get("observed")
    return None


def _is_bundle(record: dict) -> bool:
    return record.get("schema") == _BUNDLE_SCHEMA and type(record.get("version")) is int and record["version"] == 1


def _bundle_entry_record(entry: dict) -> dict:
    """Adapt a bundle entry to the existing comparison projection and its allowlists."""
    browser = _object(entry.get("browser_diagnostics"))
    network = _object(entry.get("proxy_network"))
    lifecycle = _object(entry.get("lifecycle_outcome"))
    lookup = _object(network.get("exit_lookup"))
    timings = _object(network.get("timings_ms"))
    return {
        "batch_preset": entry.get("batch_preset"),
        "status": lifecycle.get("status"),
        "workflow_outcome": lifecycle.get("workflow_outcome"),
        "elapsed_ms": lifecycle.get("elapsed_ms"),
        "metadata": {
            "causal_failure": lifecycle.get("first_causal_failure"),
            "lifecycle_timeline": lifecycle.get("timeline"),
            "initial_navigation_timeout": lifecycle.get("initial_navigation_timeout"),
            "workflow_elapsed_ms": lifecycle.get("workflow_elapsed_ms"),
            "http_status_counts": lifecycle.get("http_status_counts"),
            "main_origin_subresource_failures": lifecycle.get("main_origin_subresource_failures"),
            "transport_summary": {
                **_object(network.get("tunnel_counts")), "latency_ms": timings.get("browser_tunnel_average"),
            },
            "proxy_preflight": {
                "status": lookup.get("preflight_status"),
                "timings_ms": {
                    **_object(timings.get("preflight_tunnels")), "total": timings.get("preflight_total"),
                },
                "checks": {
                    **{name: {"confirmed": True, "status": "passed", "observed": lookup.get(name)}
                       for name in ("country", "asn")},
                    "sticky": {"status": _object(network.get("proxy")).get("sticky_check")},
                    "latency": {
                        "source": timings.get("preflight_tunnel_source"),
                        "tunnel_ms": timings.get("preflight_tunnel_latency"),
                    },
                },
            },
            "browser_diagnostic": {
                "status": browser.get("fixture_status"),
                "observation_status": browser.get("fixture_observation_status"),
                "checks": {
                    **_object(browser.get("request_headers_vs_navigator")),
                    **_object(browser.get("environment_checks")),
                },
                "capabilities": browser.get("capabilities"),
                "fixture_http_protocols": network.get("negotiated_http"),
                "connection_timings_ms": network.get("connection_timings_ms"),
                "recorded_observations": browser.get("recorded_observations"),
                "minimum_clock_skew_ms": browser.get("minimum_clock_skew_ms"),
            },
        },
    }


def _project(record: dict) -> dict:
    """Project approved fields before producing any side-by-side output."""
    if _is_bundle(record):
        record = _bundle_entry_record(_object(record.get("session")))
    is_session = "metadata" in record
    current_session = False
    if is_session:
        epoch = record.get("epoch")
        attempt = record.get("attempt_number")
        session_evidence = _current_evidence(record.get("metadata"), environment_epoch=epoch)
        current_session = session_evidence is not None
        metadata = session_evidence or {}
        diagnostic = _current_evidence(metadata.get("browser_diagnostic"), epoch=epoch, attempt=attempt) or {}
        preflight = _current_evidence(metadata.get("proxy_preflight"), epoch=epoch, attempt=attempt) or {}
    else:
        metadata = {}
        diagnostic = record
        preflight = _object(record.get("preflight"))

    causal_failure = _object(metadata.get("causal_failure"))
    step_failure = _object(metadata.get("step_failure"))
    broker_failure = first_proxy_tunnel_failure(metadata, causal_failure or step_failure or None, None) or {}
    failures = (broker_failure, causal_failure, step_failure, _object(diagnostic.get("first_causal_failure")), diagnostic)
    first_failure = next((
        safe for item in failures if item.get("reason") is not None
        and (safe := public_lifecycle_failure(item)) is not None and safe["reason"] is not None
    ), None)
    statuses = {
        "session": _choice(record.get("status"), _SESSION_STATES) if is_session else None,
        "workflow": _choice(record.get("workflow_outcome"), _WORKFLOW_STATES) if current_session else None,
        "diagnostic": _choice(diagnostic.get("status"), _REPORT_STATES),
        "observation": _choice(diagnostic.get("observation_status"), ("complete", "unavailable")),
        "preflight": _choice(preflight.get("status"), _PREFLIGHT_STATES),
        "failure_reason": first_failure["reason"] if first_failure else None,
    }
    preflight_checks = _object(preflight.get("checks"))
    country = _confirmed_lookup(preflight_checks.get("country"))
    asn = _confirmed_lookup(preflight_checks.get("asn"))
    lookup = {
        "country": country if isinstance(country, str) and re.fullmatch(r"[A-Z]{2}", country) else None,
        "asn": asn if type(asn) is int and 1 <= asn <= 4_294_967_295 else None,
        "sticky_check": _choice(_object(preflight_checks.get("sticky")).get("status"), _CHECK_STATES),
    }
    protocols = _object(diagnostic.get("fixture_http_protocols"))
    latency = _object(preflight_checks.get("latency"))
    timings = _object(preflight.get("timings_ms"))
    runtime_evidence = metadata if is_session else diagnostic
    transport = public_transport_summary(runtime_evidence.get("transport_summary")) or {}
    timings_ms = {
        "browser_elapsed": public_duration_ms(record.get("elapsed_ms")) if current_session or not is_session else None,
        "workflow_elapsed": public_duration_ms(runtime_evidence.get("workflow_elapsed_ms")),
        "transport_tunnel_mean": transport.get("latency_ms"),
        "preflight_total": public_duration_ms(timings.get("total")),
        "preflight_target_tunnel": (
            public_duration_ms(latency.get("tunnel_ms")) if latency.get("source") == "target_tls" else None
        ),
        "preflight_lookup_tunnel": (
            public_duration_ms(latency.get("tunnel_ms")) if latency.get("source") == "lookup" else None
        ),
        "minimum_clock_skew": public_duration_ms(diagnostic.get("minimum_clock_skew_ms")),
    }
    diagnostic_checks = _object(diagnostic.get("checks"))
    return {
        "batch_preset": preset_details(record.get("batch_preset")) or {},
        "outcomes": statuses,
        "exit_lookup": lookup,
        "protocols": {name: _choice(protocols.get(name), _PROTOCOLS) for name in ("document", "echo")},
        "timings_ms": timings_ms,
        "preflight_probe_timings_ms": public_probe_timings(timings),
        "http_status_counts": public_http_status_counts(runtime_evidence.get("http_status_counts")),
        "main_origin_subresource_failures": public_transport_checks(runtime_evidence).get(
            "main_origin_subresource_failures",
        ),
        "tunnel_counts": {name: transport.get(name) for name in TUNNEL_COUNT_FIELDS},
        "first_causal_failure": first_failure,
        "lifecycle": public_lifecycle_timeline(metadata.get("lifecycle_timeline"))
        or public_lifecycle_timeline(diagnostic.get("lifecycle_timeline")),
        "initial_navigation_timeout": public_initial_navigation_timeout(
            runtime_evidence.get("initial_navigation_timeout"),
        ),
        "navigation_wait": next((
            wait for item in failures if (wait := public_navigation_wait(item.get("navigation_wait"))) is not None
        ), None),
        "tunnel_wait": next((
            wait for item in failures if (wait := public_tunnel_wait(item.get("tunnel_wait"))) is not None
        ), None),
        "diagnostic_checks": {name: _choice(diagnostic_checks.get(name), _CHECK_STATES) for name in FIXTURE_CHECKS},
        "capabilities": public_capabilities(diagnostic.get("capabilities")) or {},
        "connection_timings_ms": public_connection_timings(diagnostic.get("connection_timings_ms")) or {},
        "recorded_observations": public_recorded_observations(diagnostic.get("recorded_observations")) or {},
    }


def _compare_values(baseline, candidate) -> dict:
    if baseline is None or candidate is None:
        comparison = "unknown"
    else:
        comparison = "unchanged" if baseline == candidate else "changed"
    return {"baseline": baseline, "candidate": candidate, "comparison": comparison}


def _compare_milliseconds(baseline, candidate) -> dict:
    delta = round(candidate - baseline, 3) if baseline is not None and candidate is not None else None
    return {**_compare_values(baseline, candidate), "delta_ms": delta}


def _compare_counts(baseline: int | None, candidate: int | None) -> dict:
    delta = candidate - baseline if baseline is not None and candidate is not None else None
    return {**_compare_values(baseline, candidate), "delta_count": delta}


def _compare_fields(baseline: dict, candidate: dict, compare=_compare_values) -> dict:
    return {
        field: compare(baseline.get(field), candidate.get(field))
        for field in sorted(baseline.keys() | candidate.keys())
    }


def _measured_count(counts: dict | None, group: str, name: str) -> int | None:
    return counts.get(group, {}).get(name, 0) if counts is not None else None


def _compare_count_groups(baseline: dict | None, candidate: dict | None) -> dict:
    """A missing bucket is zero only when that run recorded counters."""
    before, after = baseline or {}, candidate or {}
    return {
        group: {
            name: _compare_counts(_measured_count(baseline, group, name), _measured_count(candidate, group, name))
            for name in sorted(before.get(group, {}).keys() | after.get(group, {}).keys())
        }
        for group in sorted(before.keys() | after.keys())
    }


def _compare_probe_timings(baseline: dict, candidate: dict) -> dict:
    result = {}
    for probe in sorted(baseline.keys() | candidate.keys()):
        before, after = baseline.get(probe, []), candidate.get(probe, [])
        result[probe] = [
            _compare_fields(left or {}, right or {}, _compare_milliseconds)
            for left, right in zip_longest(before, after)
        ]
    return result


def _compare_observations(baseline: dict, candidate: dict) -> dict:
    return {
        group: _compare_fields(baseline.get(group, {}), candidate.get(group, {}))
        for group in sorted(baseline.keys() | candidate.keys())
    }


def _navigation_offsets(timeline: dict | None) -> dict:
    return {event["event"]: event["navigation_delta_ms"] for event in (timeline or {}).get("events", [])}


def _compare_connections(baseline: dict, candidate: dict) -> dict:
    connections = {}
    for name in sorted(baseline.keys() | candidate.keys()):
        left, right = baseline.get(name, {}).copy(), candidate.get(name, {}).copy()
        connections[name] = {
            "status": _compare_values(left.pop("status", None), right.pop("status", None)),
            **_compare_fields(left, right, _compare_milliseconds),
        }
    return connections


def compare_runs(baseline: dict, candidate: dict) -> dict:
    """Return redacted side-by-side observations and candidate-minus-baseline timing deltas."""
    left, right = _project(baseline), _project(candidate)
    return {
        "version": 1,
        **({"batch_preset": _compare_fields(left["batch_preset"], right["batch_preset"])}
           if left["batch_preset"] or right["batch_preset"] else {}),
        **{
            name: _compare_fields(left[name], right[name])
            for name in ("outcomes", "exit_lookup", "protocols", "diagnostic_checks", "capabilities")
        },
        "timings_ms": _compare_fields(left["timings_ms"], right["timings_ms"], _compare_milliseconds),
        "preflight_probe_timings_ms": _compare_probe_timings(
            left["preflight_probe_timings_ms"], right["preflight_probe_timings_ms"],
        ),
        "http_status_counts": _compare_count_groups(left["http_status_counts"], right["http_status_counts"]),
        "main_origin_subresource_failures": _compare_count_groups(
            left["main_origin_subresource_failures"], right["main_origin_subresource_failures"],
        ),
        "tunnel_counts": _compare_fields(left["tunnel_counts"], right["tunnel_counts"], _compare_counts),
        "connection_timings_ms": _compare_connections(left["connection_timings_ms"], right["connection_timings_ms"]),
        "recorded_observations": _compare_observations(left["recorded_observations"], right["recorded_observations"]),
        "first_causal_failure": {"baseline": left["first_causal_failure"], "candidate": right["first_causal_failure"]},
        "initial_navigation_timeout": _compare_values(
            left["initial_navigation_timeout"], right["initial_navigation_timeout"],
        ),
        "navigation_wait": {"baseline": left["navigation_wait"], "candidate": right["navigation_wait"]},
        "tunnel_wait": {"baseline": left["tunnel_wait"], "candidate": right["tunnel_wait"]},
        "lifecycle": {
            "baseline": left["lifecycle"],
            "candidate": right["lifecycle"],
            "navigation_deltas_ms": _compare_fields(
                _navigation_offsets(left["lifecycle"]), _navigation_offsets(right["lifecycle"]), _compare_milliseconds,
            ),
        },
    }


def _read_export(path: Path) -> dict:
    with path.open("rb") as stream:
        raw = stream.read(_MAX_EXPORT_BYTES + 1)
    if len(raw) > _MAX_EXPORT_BYTES:
        raise ValueError("Export exceeds size limit")
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("Export must be a JSON object")
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare two report files or one diagnostic bundle with a baseline. "
        "Timing differences are candidate minus baseline."
    )
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path, nargs="?")
    args = parser.parse_args(argv)
    try:
        baseline = _read_export(args.baseline)
        candidate = _read_export(args.candidate) if args.candidate else None
    except (OSError, ValueError, RecursionError):
        parser.error("Inputs must be readable JSON objects of at most 16 MiB each.")
    if candidate is None:
        original = _object(baseline.get("baseline"))
        selected = _object(baseline.get("session"))
        if not _is_bundle(baseline) or not original or not selected:
            parser.error("Provide two reports or a diagnostic bundle that contains a baseline.")
        baseline, candidate = _bundle_entry_record(original), _bundle_entry_record(selected)
    print(json.dumps(compare_runs(baseline, candidate), indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
