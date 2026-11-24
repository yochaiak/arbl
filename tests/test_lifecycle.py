import json

import pytest
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import never_cancel
from test_platform import client, create
from test_platform import db as db

from abrl import lifecycle, platform_service, session_runner
from abrl.browser import execute_workflow
from abrl.db import TargetVersion, WorkflowVersion
from abrl.lifecycle import LifecycleTimeline, public_lifecycle_timeline


def test_lifecycle_timeline_orders_events_and_derives_signed_offsets(monkeypatch):
    snapshots = iter(value * 1_000_000 for value in [0, 5, 10, 20, 25, 30, 35])
    monkeypatch.setattr(lifecycle, "monotonic_ns", lambda: next(snapshots))
    timeline = LifecycleTimeline()
    assert timeline.view() is None
    timeline.mark("preflight_completed")
    assert timeline.view() == {
        "version": 1, "events": [{"event": "preflight_completed", "elapsed_ms": 5, "navigation_delta_ms": None}],
    }
    timeline.mark("browser_launched")
    timeline.mark("navigation_started")
    timeline.mark("navigation_started")
    timeline.mark("last_subresource_completed_before_load", latest=True)
    timeline.mark("last_subresource_completed_before_load", latest=True)
    timeline.mark("page_loaded")
    assert timeline.view() == {
        "version": 1,
        "events": [
            {"event": "preflight_completed", "elapsed_ms": 5, "navigation_delta_ms": -15},
            {"event": "browser_launched", "elapsed_ms": 10, "navigation_delta_ms": -10},
            {"event": "navigation_started", "elapsed_ms": 20, "navigation_delta_ms": 0},
            {"event": "last_subresource_completed_before_load", "elapsed_ms": 30, "navigation_delta_ms": 10},
            {"event": "page_loaded", "elapsed_ms": 35, "navigation_delta_ms": 15},
        ],
    }


def test_public_lifecycle_timeline_projects_only_fixed_numeric_milestones():
    view = public_lifecycle_timeline({
        "version": 1,
        "origin": "https://private.example/",
        "events": [
            {"event": "document_committed", "elapsed_ms": 20, "cookie": "private_cookie"},
            {"event": "navigation_started", "elapsed_ms": 10, "navigation_delta_ms": "secret"},
            {"event": "private_candidate", "elapsed_ms": 12},
            {"event": "first_interaction", "elapsed_ms": True, "password": "private_password"},
            {"event": "preflight_completed", "elapsed_ms": "192.0.2.7"},
            {"event": "browser_launched", "elapsed_ms": -1},
            {"event": "workflow_completed", "elapsed_ms": 10**10},
            {"event": "document_committed", "elapsed_ms": 30},
        ],
    })
    assert view == {
        "version": 1,
        "events": [
            {"event": "navigation_started", "elapsed_ms": 10, "navigation_delta_ms": 0},
            {"event": "document_committed", "elapsed_ms": 20, "navigation_delta_ms": 10},
        ],
    }


@pytest.mark.integration
async def test_loopback_lifecycle_tracks_initial_load_subresources_and_interaction(chromium, fixture):
    origin, visited, _ = fixture
    timeline = LifecycleTimeline()
    timeline.mark("preflight_completed")
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[
            {"action": "navigate", "url": "/lab?lifecycle=private-query"},
            {"action": "wait_for_text", "selector": "#load-ready", "value": "Ready"},
            {"action": "fill", "selector": "#name", "value": "private-input"},
            {"action": "click", "selector": "#submit"},
            {"action": "assert_text", "selector": "#success", "value": "Sample saved"},
        ],
        cancel=never_cancel,
        lifecycle_timeline=timeline,
    )
    assert result.outcome == "completed", result.error
    view = result.metadata["lifecycle_timeline"]
    events = view["events"]
    assert [event["elapsed_ms"] for event in events] == sorted(event["elapsed_ms"] for event in events)
    order = {event["event"]: index for index, event in enumerate(events)}
    for milestones in (
        ("preflight_completed", "browser_launched", "navigation_started", "document_committed"),
        ("document_committed", "dom_content_loaded", "page_loaded", "first_post_load_step", "workflow_completed"),
        ("document_committed", "first_subresource_completed", "last_subresource_completed_before_load", "page_loaded"),
        ("page_loaded", "first_interaction", "workflow_completed"),
    ):
        positions = [order[name] for name in milestones]
        assert positions == sorted(positions), view
    nav_elapsed = events[order["navigation_started"]]["elapsed_ms"]
    assert all(event["navigation_delta_ms"] == event["elapsed_ms"] - nav_elapsed for event in events)
    assert events[order["preflight_completed"]]["navigation_delta_ms"] < 0
    assert events[order["browser_launched"]]["navigation_delta_ms"] <= 0
    assert [path for _, path, _ in visited] == [
        "/lab?lifecycle=private-query", "/lab/form?lifecycle-asset=private-asset", "/lab/form",
    ]
    rendered = json.dumps(view)
    assert all(secret not in rendered for secret in (origin, "private-query", "private-asset", "private-input", "#name"))


@pytest.mark.integration
async def test_loopback_lifecycle_aligns_classified_failure_with_navigation(chromium, fixture):
    origin, _, _ = fixture
    result = await execute_workflow(
        origin=origin,
        resource_origins=[],
        steps=[{"action": "navigate", "url": "/lab/next"}],
        cancel=never_cancel,
    )
    assert result.outcome == "blocked"
    assert result.metadata["causal_failure"]["reason"] == "site_denial"
    events = {event["event"]: event for event in result.metadata["lifecycle_timeline"]["events"]}
    assert events["navigation_started"]["navigation_delta_ms"] == 0
    assert events["failure_observed"]["navigation_delta_ms"] >= 0
    assert "workflow_completed" not in events
    assert "site_denial" not in json.dumps(events)


@pytest.mark.integration
async def test_session_details_retains_lifecycle_for_a_loopback_workflow(db, fixture, chromium, monkeypatch, tmp_path):
    origin, _, _ = fixture
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "runtime")
    monkeypatch.setattr(platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}])
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": origin}
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {**workflow.config, "steps": [{"action": "navigate", "url": "/lab?private-target"}]}
    create(db, mode="live", capture_javascript=False, artifact_policy="none")
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None
    await session_runner.execute(work)
    with client() as api:
        response = api.get(f"/api/v1/sessions/{work.id}")
    assert response.status_code == 200
    session = response.json()
    assert session["status"] == "completed", session["error"]
    view = session["metadata"]["lifecycle_timeline"]
    assert view == public_lifecycle_timeline(view)
    events = {event["event"]: event for event in view["events"]}
    assert events["navigation_started"]["navigation_delta_ms"] == 0
    assert events["document_committed"]["navigation_delta_ms"] >= 0
    assert events["workflow_completed"]["navigation_delta_ms"] >= events["document_committed"]["navigation_delta_ms"]
    assert "preflight_completed" not in events
    assert "private-target" not in json.dumps(view)
