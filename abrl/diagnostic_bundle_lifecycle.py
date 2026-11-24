"""Allowlisted lifecycle evidence for the portable diagnostic bundle."""

from .browser_network_trace import public_http_status_counts, public_transport_checks
from .connect_telemetry import public_navigation_connects
from .diagnostic_fixture import DIAGNOSTIC_PROBE_FAILURES
from .lifecycle import (
    MAX_LIFECYCLE_ELAPSED_MS,
    public_duration_ms,
    public_initial_navigation_timeout,
    public_lifecycle_timeline,
    public_navigation_wait,
    public_tunnel_wait,
)
from .network import PROXY_TRANSPORT_REASONS, first_proxy_tunnel_failure
from .schemas import CONTROL_ACTIONS, NAVIGATION_ACTIONS, OBSERVATION_ACTIONS

_SESSION_STATES = (
    "queued", "starting", "preparing", "ready", "running", "awaiting_operator", "stopping",
    "completed", "failed", "blocked", "cancelled", "needs_reconciliation",
)
_OUTCOMES = ("completed", "failed", "blocked", "cancelled", "needs_reconciliation", "not_started")
_PHASES = ("preflight", "startup", "proxy", "profile", "automated_preparation", "manual_preparation",
           "workflow", "diagnostic", "cleanup")
_REASONS = frozenset({
    *PROXY_TRANSPORT_REASONS,
    "attempt_failed", "authentication", "asn_not_allowed", "binding_unavailable", "browser_crash",
    "browser_error", "budget_exhausted", "capacity_exhausted", "capacity_unverified", "check_mismatch",
    "cleanup_failed", "containment_unavailable", "country_mismatch", "diagnostic_echo_http_error", "diagnostic_echo_network_error",
    "diagnostic_fixture_error", "diagnostic_proxy_binding_required", "engine_auth_failed", "engine_error", "engine_unavailable",
    "execution_interrupted", "fixture_configuration", "fixture_failed", "hosting_provider",
    "incompatible_license", "invalid_configuration", "invalid_observations", "launch_failed", "launch_timeout",
    "license_or_launch_error",
    "main_document_http_error", "missing_binary", "missing_license", "navigation_failed", "navigation_timeout",
    "preflight", "preflight_timeout", "profile_incompatible", "profile_restore_failed", "profile_store_unavailable",
    "profile_unavailable", "proxy_lease_expired",
    "proxy_lease_unavailable", "quota",
    "scope_denied", "selector_timeout", "service_worker_not_allowed", "site_challenge", "site_denial", "startup_transient", "step_failed",
    "timeout", "unexpected_dialog", "unexpected_popup", "unexpected_redirect", "unsupported_adapter",
    "unsupported_operation", "unverified_license",
    "viewport_mismatch",
})
_ACTIONS = NAVIGATION_ACTIONS | OBSERVATION_ACTIONS | CONTROL_ACTIONS | {
    "check", "click", "close_tab", "expect_popup", "fill", "hover", "press", "select", "success_check",
    "switch_tab", "uncheck",
}
_MAX_REQUEST_COUNT = 100_000


def _choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def _number(value, minimum, maximum):
    return value if type(value) is int and minimum <= value <= maximum else None


def public_lifecycle_failure(record: object) -> dict | None:
    """Project fixed failure evidence shared by diagnostic reports and bundles."""
    if not isinstance(record, dict) or not record:
        return None
    result = {
        "phase": _choice(record.get("phase"), _PHASES),
        "reason": _choice(record.get("reason"), _REASONS),
        "action": _choice(record.get("action"), _ACTIONS),
        "step": _number(record.get("step"), 1, _MAX_REQUEST_COUNT),
        "http_status": _number(record.get("status", record.get("http_status")), 100, 599),
        "elapsed_ms": _number(record.get("elapsed_ms"), 0, MAX_LIFECYCLE_ELAPSED_MS),
    }
    path = record.get("step_path")
    if isinstance(path, list) and 1 <= len(path) <= 32 and all(
        _number(part, 1, _MAX_REQUEST_COUNT) is not None or _choice(part, ("then", "else", "block", "repeat"))
        for part in path
    ):
        result["step_path"] = path
    if isinstance(navigation := record.get("navigation"), dict):
        result["navigation"] = {
            **{key: _number(navigation.get(key), 0, _MAX_REQUEST_COUNT)
               for key in ("requests", "responses", "commits")},
            "last_status": _number(navigation.get("last_status"), 100, 599),
        }
    if (navigation_wait := public_navigation_wait(record.get("navigation_wait"))) is not None:
        result["navigation_wait"] = navigation_wait
    if (tunnel_wait := public_tunnel_wait(record.get("tunnel_wait"))) is not None:
        result["tunnel_wait"] = tunnel_wait
    if (probe_failure := _choice(record.get("probe_failure"), DIAGNOSTIC_PROBE_FAILURES)) is not None:
        result["probe_failure"] = probe_failure
    return result


def _first_failure(session, metadata: dict) -> dict | None:
    failure = next((
        record for key in ("causal_failure", "step_failure")
        if isinstance(record := metadata.get(key), dict) and record
    ), None)
    if session.outcome not in ("completed", "cancelled"):
        failure = first_proxy_tunnel_failure(metadata, failure, session.elapsed_ms) or failure
    if failure is None:
        diagnostic = metadata.get("browser_diagnostic")
        if (
            isinstance(diagnostic, dict)
            and diagnostic.get("epoch") == session.epoch
            and diagnostic.get("attempt") == session.attempt_number
            and diagnostic.get("status") == "failed"
        ):
            failure = {"phase": "diagnostic", "reason": diagnostic.get("reason")}
    return public_lifecycle_failure(failure)


def _failure_stage(failure: dict | None, timeline: dict | None, outcome: str | None) -> str | None:
    if failure is None:
        return "unknown" if outcome in {"blocked", "failed", "needs_reconciliation"} else None
    events = {event["event"]: index for index, event in enumerate((timeline or {}).get("events", []))}
    if (observed := events.get("failure_observed")) is None:
        return "pre_load" if failure["phase"] in {"preflight", "startup"} else "unknown"
    if events.get("first_interaction", observed) < observed:
        return "post_action"
    if (started := events.get("navigation_started")) is None or observed < started:
        return "pre_load"
    return "post_load" if events.get("page_loaded", observed) < observed else "initial_load"


def bundle_lifecycle(session, metadata: dict) -> dict:
    """Project only current-attempt status, bounded counts, and ordered milestones."""
    timeline = public_lifecycle_timeline(metadata.get("lifecycle_timeline"))
    failure = _first_failure(session, metadata)
    status = _choice(session.status, _SESSION_STATES)
    outcome = _choice(session.outcome, _OUTCOMES)
    return {
        "status": status,
        "outcome": outcome,
        "workflow_outcome": _choice(session.workflow_outcome, _OUTCOMES),
        "elapsed_ms": _number(session.elapsed_ms, 0, MAX_LIFECYCLE_ELAPSED_MS),
        "workflow_elapsed_ms": public_duration_ms(metadata.get("workflow_elapsed_ms")),
        "timeline": timeline,
        "initial_navigation_timeout": public_initial_navigation_timeout(metadata.get("initial_navigation_timeout")),
        "navigation_connects": public_navigation_connects(metadata.get("navigation_connects")),
        "failure_stage": _failure_stage(failure, timeline, outcome or status),
        "first_causal_failure": failure,
        "http_status_counts": public_http_status_counts(metadata.get("http_status_counts")),
        "main_origin_subresource_failures": public_transport_checks(metadata).get("main_origin_subresource_failures"),
    }
