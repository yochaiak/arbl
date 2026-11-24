import json
from types import SimpleNamespace

import pytest
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel

from abrl.browser import execute_workflow
from abrl.diagnostic_bundle_lifecycle import bundle_lifecycle


def _session(status="blocked"):
    return SimpleNamespace(
        status=status, outcome=status, workflow_outcome=status, elapsed_ms=51, epoch=4, attempt_number=2,
    )


def _timeline(*milestones):
    return {"version": 1, "events": [{"event": event, "elapsed_ms": clock} for event, clock in milestones]}


@pytest.mark.parametrize(("phase", "milestones", "stage"), [
    ("preflight", (("preflight_completed", 10),), "pre_load"),
    ("startup", (("browser_launched", 12), ("failure_observed", 15)), "pre_load"),
    ("workflow", (("navigation_started", 5), ("failure_observed", 6)), "initial_load"),
    ("workflow", (("navigation_started", 5), ("page_loaded", 8), ("failure_observed", 8)), "post_load"),
    ("workflow", (("navigation_started", 5), ("first_interaction", 8), ("failure_observed", 8)), "post_action"),
    ("workflow", (("navigation_started", 5), ("page_loaded", 8)), "unknown"),
    ("workflow", (("navigation_started", 5), ("failure_observed", 8), ("first_interaction", 8)), "initial_load"),
])
def test_detection_stage_uses_ordered_markers_and_keeps_unmeasured_stage_unknown(phase, milestones, stage):
    evidence = {"causal_failure": {"phase": phase, "reason": "site_denial"}, "lifecycle_timeline": _timeline(*milestones)}
    assert bundle_lifecycle(_session(), evidence)["failure_stage"] == stage


def test_lifecycle_export_keeps_only_fixed_labels_and_bounded_values():
    metadata = {
        "causal_failure": {
            "phase": "workflow", "reason": "site_denial", "action": "navigate", "step": 1,
            "status": 403, "elapsed_ms": 13, "error": "private-cookie",
            "url": "https://private.example/?token=private-auth", "step_path": ["private-user-note"],
            "navigation": {"requests": 3, "responses": True, "commits": 1, "last_status": 403, "url": "private-nested"},
        },
        "step_failure": {"reason": "browser_error"},
        "lifecycle_timeline": {
            "version": 1, "events": [
                {"event": "failure_observed", "elapsed_ms": 13, "raw_candidate": "private-ice"},
                {"event": "navigation_started", "elapsed_ms": 5, "navigation_delta_ms": "private-manipulated"},
                {"event": "private-identifier", "elapsed_ms": 6},
            ],
        },
        "http_status_counts": {
            "main_frame": {"200": 1, "403": 2, "192.0.2.1": 8, "500": True, "999": 10, "501": 1_000_000},
            "subresource": {"404": 1, "502": -1},
            "private-header": {"200": 1},
        },
        "workflow_elapsed_ms": 31.25,
        "main_origin_subresource_failures": {"image": {"http_4xx": 1, "private-reason": 5}},
        "authorization": "private-auth",
    }
    export = bundle_lifecycle(_session(), metadata)
    assert export["first_causal_failure"] == {
        "phase": "workflow", "reason": "site_denial", "action": "navigate", "step": 1,
        "http_status": 403, "elapsed_ms": 13,
        "navigation": {"requests": 3, "responses": None, "commits": 1, "last_status": 403},
    }
    assert export["http_status_counts"] == {"main_frame": {"200": 1, "403": 2}, "subresource": {"404": 1}}
    assert export["workflow_elapsed_ms"] == 31.25
    assert export["main_origin_subresource_failures"] == {"image": {"http_4xx": 1}}
    assert export["timeline"]["events"][-1]["navigation_delta_ms"] == 8
    assert not any(secret in json.dumps(export) for secret in ("192.0.2.1", "private", "authorization"))


def test_missing_and_legacy_evidence_are_distinct_from_measured_empty_counts():
    assert bundle_lifecycle(_session(), {})["failure_stage"] == "unknown"
    assert bundle_lifecycle(_session("completed"), {})["http_status_counts"] is None
    assert bundle_lifecycle(_session("completed"), {})["workflow_elapsed_ms"] is None
    assert bundle_lifecycle(_session("completed"), {})["failure_stage"] is None
    assert bundle_lifecycle(_session("completed"), {})["initial_navigation_timeout"] is None
    measured = {"http_status_counts": {"main_frame": {}, "subresource": {}}}
    assert bundle_lifecycle(_session("completed"), measured)["http_status_counts"] == measured["http_status_counts"]
    stale_diagnostic = {"browser_diagnostic": {"epoch": 3, "attempt": 1, "status": "failed", "reason": "check_mismatch"}}
    assert bundle_lifecycle(_session(), stale_diagnostic)["first_causal_failure"] is None
    unknown = bundle_lifecycle(_session(), {"causal_failure": {"phase": "private-phase", "reason": "private-cause"}})
    assert unknown["first_causal_failure"]["reason"] is None
    assert unknown["failure_stage"] == "unknown"


@pytest.mark.parametrize(("phase", "reason"), [
    ("startup", "containment_unavailable"),
    ("workflow", "service_worker_not_allowed"),
])
def test_containment_failure_is_reported_without_internal_metadata(phase, reason):
    failure = bundle_lifecycle(_session(), {"causal_failure": {
        "phase": phase, "reason": reason, "endpoint": "ws://localhost/private-runtime-endpoint",
    }})["first_causal_failure"]
    assert failure["phase"] == phase and failure["reason"] == reason
    assert "private-runtime-endpoint" not in json.dumps(failure)


def test_navigation_wait_export_is_bounded_and_redacted():
    wait = {
        "stage": "initial_dom_content_loaded", "deadline": "step", "stage_elapsed_ms": 51,
        "load_timeout_ms": 5000, "post_navigation_wait_ms": 100_000, "url": "private-navigation-url",
        "load_timeout_source": "https://192.0.2.2/?token=private-timeout-source",
    }
    failure = {"phase": "workflow", "reason": "navigation_timeout", "action": "navigate", "navigation_wait": wait}
    export = bundle_lifecycle(_session("failed"), {"causal_failure": failure})["first_causal_failure"]
    assert export["navigation_wait"] == {
        "stage": "initial_dom_content_loaded", "deadline": "step", "stage_elapsed_ms": 51, "load_timeout_ms": 5000,
    }
    wait["stage"] = "private-stage"
    assert "navigation_wait" not in bundle_lifecycle(_session("failed"), {"causal_failure": failure})["first_causal_failure"]


@pytest.mark.parametrize("source", ["step_override", "proxy_policy", "proxy_default", "direct_default"])
def test_bundle_preserves_timeout_source_with_a_bounded_load_timeout(source):
    wait = {
        "stage": "initial_dom_content_loaded", "deadline": "step",
        "load_timeout_ms": 18_000, "load_timeout_source": source,
    }
    failure = {"phase": "workflow", "reason": "navigation_timeout", "action": "navigate", "navigation_wait": wait}
    metadata = {"causal_failure": failure}
    export = bundle_lifecycle(_session("failed"), metadata)["first_causal_failure"]
    assert export["navigation_wait"] == wait

    wait["load_timeout_ms"] = 120_001
    export = bundle_lifecycle(_session("failed"), metadata)["first_causal_failure"]
    assert export["navigation_wait"] == {"stage": wait["stage"], "deadline": wait["deadline"]}


def test_bundle_preserves_first_navigation_timeout_without_a_failure():
    configured = {"load_timeout_ms": 15_000, "load_timeout_source": "proxy_default"}
    metadata = {"initial_navigation_timeout": {**configured, "credential": "private-token"}}
    export = bundle_lifecycle(_session("completed"), metadata)
    assert export["initial_navigation_timeout"] == configured
    assert export["first_causal_failure"] is None

    metadata["initial_navigation_timeout"]["load_timeout_source"] = "https://192.0.2.1/"
    assert bundle_lifecycle(_session("completed"), metadata)["initial_navigation_timeout"] is None


@pytest.mark.parametrize(("successes", "event_observed"), [(0, True), (1, True), (0, False)])
def test_bundle_distinguishes_browser_deadline_from_proven_tunnel_timeout(successes, event_observed):
    navigation_wait = {"stage": "initial_dom_content_loaded", "deadline": "step", "stage_elapsed_ms": 5000}
    issue = {
        "reason": "proxy_tunnel_timeout", "error": "private-response",
        "tunnel_wait": {"stage": "proxy_connect", "stage_elapsed_ms": 4510, "address": "192.0.2.42"},
    }
    metadata = {
        "causal_failure": {
            "reason": "navigation_timeout", "phase": "workflow",
            "action": "navigate", "step": 1, "navigation_wait": navigation_wait,
        },
        "transport_summary": {"attempts": 2, "successes": successes},
        "transport_events": [issue] if event_observed else [],
    }
    failure = bundle_lifecycle(_session("failed"), metadata)["first_causal_failure"]
    assert failure["navigation_wait"] == navigation_wait
    if successes == 0 and event_observed:
        assert failure["phase"] == "proxy" and failure["reason"] == "proxy_tunnel_timeout"
        assert failure["tunnel_wait"] == {"stage": "proxy_connect", "stage_elapsed_ms": 4510}
    else:
        assert failure["phase"] == "workflow" and failure["reason"] == "navigation_timeout"
        assert "tunnel_wait" not in failure
    assert "192.0.2.42" not in json.dumps(failure) and "private-response" not in json.dumps(failure)


@pytest.mark.integration
async def test_bundle_places_a_post_interaction_challenge_on_the_loopback_timeline(chromium, fixture):
    origin, _, _ = fixture
    result = await execute_workflow(
        origin=origin, resource_origins=[], cancel=never_cancel,
        steps=[{"action": "navigate", "url": "/lab?challenge-link"}, {"action": "click", "selector": "#verify"}],
    )
    assert result.outcome == "blocked", result.error
    assert result.metadata["main_document_http_protocol"] == "http/1.1"
    export = bundle_lifecycle(_session(), result.metadata)
    assert export["first_causal_failure"]["reason"] == "site_challenge"
    assert export["failure_stage"] == "post_action"
    assert export["http_status_counts"]["main_frame"] == {"200": 2}
    assert not any(secret in json.dumps(export) for secret in (origin, "/lab", "#verify"))
