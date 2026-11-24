"""Integration checks for the loopback API, PostgreSQL, Redis and the native worker."""

import csv
import io
import json
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from abrl.config import API_ORIGIN, CLICKHOUSE_PASSWORD, CLICKHOUSE_URL, OPERATOR_TOKEN

pytestmark = pytest.mark.integration
BASE_URL = "http://127.0.0.1:8400"
TERMINAL = {"completed", "blocked", "cancelled"}


def _response(response, expected=200):
    assert response.status_code == expected, f"{response.request.url.path}: {response.text}"
    return response


def _eventually(read, condition, *, timeout=30, description):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = read()
        if condition(latest):
            return latest
        time.sleep(0.25)
    pytest.fail(f"Timed out waiting for {description}. Last response: {latest!r}")


@pytest.fixture(scope="module")
def api():
    assert OPERATOR_TOKEN, "Set the operator token and start make dev-fixtures before running integration tests."
    with httpx.Client(
        base_url=f"{BASE_URL}/api/v1",
        headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"},
        timeout=5,
        trust_env=False,
    ) as client:
        try:
            _response(client.get(f"{BASE_URL}/health"))
        except httpx.ConnectError:
            pytest.fail(f"Start the integration API on {BASE_URL} with make dev-fixtures.")
        _eventually(
            lambda: _response(client.get("/status")).json()["worker"],
            lambda worker: worker["ready"],
            description="the queue worker heartbeat",
        )
        yield client


@pytest.fixture(scope="module")
def starter(api):
    seed = _response(api.post(f"{BASE_URL}/test/seed")).json()
    workflows = {
        workflow["name"]: workflow
        for workflow in _response(api.get("/workflows")).json()
        if workflow["id"] in seed["workflow_ids"]
    }
    return seed["target_id"], workflows


def _create_run(api, target_id, workflow_id, *, mode="live", repetitions=1, seed=42):
    return _response(
        api.post(
            "/runs",
            json={
                "target_id": target_id,
                "workflow_id": workflow_id,
                "mode": mode,
                "repetitions": repetitions,
                "seed": seed,
            },
        ),
        expected=201,
    ).json()["id"]


def _read_report(api, run_id):
    return _response(api.get(f"/runs/{run_id}/report")).json()


def _wait_terminal(api, run_id):
    return _eventually(
        lambda: _read_report(api, run_id),
        lambda report: report["status"] in TERMINAL,
        description=f"run {run_id} to finish",
    )


@pytest.fixture(scope="module")
def live_reports(api, starter):
    target_id, workflows = starter
    reports = {}
    for name in ("Sample form", "Repeat visit"):
        run_id = _create_run(api, target_id, workflows[name]["id"])
        reports[name] = _wait_terminal(api, run_id)
    return reports


@pytest.mark.parametrize("workflow", ["Sample form", "Repeat visit"])
def test_live_browser_workflows(api, live_reports, workflow):
    report = live_reports[workflow]
    assert report["status"] == "completed", report["trials"]
    assert report["mode"] == "live"
    assert report["target_mode"] == "browser_only"
    assert report["completion"]["successes"] == report["completion"]["total"] == 1, report["trials"]
    assert report["verdicts"] == {}
    assert report["counts"]["verified"] == 0
    assert report["recommendation"]["status"] == "pilot"

    trials = _response(api.get(f"/runs/{report['run_id']}/trials")).json()
    assert len(trials) == 1
    trial = trials[0]
    assert trial["outcome"] == "completed"
    assert trial["attempts"] == 1
    assert trial["verification_status"] == "not_collected"
    assert trial["verdict"] is None
    assert trial["environment"]["browser_version"]


@pytest.mark.parametrize("mode", ["round_robin", "seeded"])
def test_live_batch_uses_pinned_responsive_presets(api, starter, mode):
    target_id, workflows = starter
    batch = _response(api.post("/batches", json={
        "target_id": target_id,
        "workflow_id": workflows["Landing check"]["id"],
        "idempotency_key": uuid4().hex,
        "count": 4,
        "artifact_policy": "none",
        "capture_javascript": False,
        "seed": 96,
        "batch_presets": {
            "mode": mode, "presets": ["desktop_1440", "compact_390"],
            "locales": ["en-US", "en-GB"],
        },
    }), expected=201).json()
    batch_path = f"/batches/{batch['id']}"
    items = _response(api.get(f"{batch_path}/items")).json()["items"]
    paths = [f"/sessions/{item['session_id']}" for item in sorted(items, key=lambda item: item["ordinal"])]
    sessions = _eventually(
        lambda: [_response(api.get(path)).json() for path in paths],
        lambda rows: len(rows) == 4 and all(row["finished_at"] for row in rows),
        description="the preset batch to finish",
    )
    assert [row["status"] for row in sessions] == ["completed"] * 4
    assert len({row["profile_id"] for row in sessions}) == 4
    pairs = [(row["batch_preset"]["id"], row["batch_preset"]["locale"]) for row in sessions]
    expected = [
        ("desktop_1440", "en-US"), ("desktop_1440", "en-GB"),
        ("compact_390", "en-US"), ("compact_390", "en-GB"),
    ]
    if mode == "round_robin":
        assert pairs == expected
    else:
        assert sorted(pairs) == sorted(expected)
    for session, path in zip(sessions, paths, strict=True):
        preset = session["batch_preset"]
        assert preset["seed"] == 96 and preset["mode"] == mode
        assert session["metadata"]["device_mode"] == "responsive_viewport"
        assert session["metadata"]["viewport"] == preset["viewport"]
        exported = _response(api.get(f"{path}/diagnostic-bundle")).json()
        assert exported["session"]["batch_preset"] == preset


def test_exports_and_event_stream(api, live_reports):
    report = live_reports["Sample form"]
    run_id = report["run_id"]
    path = f"/runs/{run_id}/export"
    exported = _response(api.get(path, params={"format": "json"}))
    assert exported.json() == report
    assert f"abrl-{run_id}.json" in exported.headers["content-disposition"]

    csv_text = _response(api.get(path, params={"format": "csv"})).text
    csv_rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert len(csv_rows) == 1
    assert csv_rows[0]["mode"] == "live"
    assert csv_rows[0]["outcome"] == "completed"
    assert csv_rows[0]["verdict"] == ""

    html = _response(api.get(path, params={"format": "html"})).text
    assert report["config_sha256"] in html
    pdf = _response(api.get(path, params={"format": "pdf"}))
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-")

    events = []
    deadline = time.monotonic() + 10
    with api.stream("GET", f"/runs/{run_id}/events") as stream:
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        for line in stream.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:])["kind"])
                if events[-1] == "run_completed":
                    break
            if len(events) > 30 or time.monotonic() >= deadline:
                pytest.fail(f"No completion event in stream. Received: {events!r}")
    assert events == ["run_queued", "run_started", "trial_started", "trial_completed", "run_completed"]


def test_live_launch_and_version_history(api):
    target_data = {"name": "Integration target", "origin": API_ORIGIN, "authorized": True}
    target = _response(api.post("/targets", json=target_data), expected=201).json()
    workflow_data = {
        "name": "Integration workflow",
        "steps": [
            {"action": "navigate", "url": "/lab"},
            {"action": "assert_visible", "selector": "#lab-ready"},
        ],
    }
    workflow = _response(api.post("/workflows", json=workflow_data), expected=201).json()
    rejected = api.post(
        "/runs", json={"target_id": target["id"], "workflow_id": workflow["id"], "mode": "demo"}
    )
    assert rejected.status_code == 422
    first_run = _create_run(api, target["id"], workflow["id"])
    before = _wait_terminal(api, first_run)
    assert before["mode"] == "live"
    assert before["completion"]["successes"] == 1

    for resource, original, payload in (
        ("targets", target, target_data),
        ("workflows", workflow, workflow_data),
    ):
        path = f"/{resource}/{original['id']}"
        updated = _response(api.put(path, json={**payload, "name": f"{payload['name']} v2"})).json()
        assert updated["version"] == 2
        versions = _response(api.get(f"{path}/versions")).json()
        assert [version["version"] for version in versions] == [2, 1]
        assert versions[-1]["name"] == payload["name"]

    after = _read_report(api, first_run)
    assert after == before
    assert after["target_version"] == after["workflow_version"] == 1


def test_requires_authentication_and_keeps_registered_navigation_scope(api):
    unauthorized = httpx.get(f"{BASE_URL}/api/v1/targets", timeout=5, trust_env=False)
    assert unauthorized.status_code == 401

    # The run is rejected before it can contact a destination outside the registration.
    remote = _response(
        api.post(
            "/targets",
            json={
                "name": "Integration target with exact scope",
                "origin": "https://registered.test",
            },
        ),
        expected=201,
    ).json()
    assert remote["authorized"] and remote["live_ready"]
    workflow = _response(
        api.post("/workflows", json={
            "name": "Integration out-of-scope workflow",
            "steps": [{"action": "navigate", "url": "https://sub.registered.test/"}],
        }),
        expected=201,
    ).json()
    blocked = api.post(
        "/runs",
        json={
            "target_id": remote["id"],
            "workflow_id": workflow["id"],
            "mode": "live",
            "repetitions": 1,
        },
    )
    assert blocked.status_code == 409
    assert "registered target scope" in blocked.json()["detail"]


def test_cancel_stops_in_progress_workflow(api, starter):
    workflow = _response(
        api.post(
            "/workflows",
            json={
                "name": "Integration cancellation",
                "steps": [
                    {"action": "navigate", "url": "/lab"},
                    {"action": "wait_for", "selector": "#never-present", "timeout_ms": 15000},
                    {"action": "assert_visible", "selector": "#lab-ready"},
                ],
            },
        ),
        expected=201,
    ).json()
    run_id = _create_run(api, starter[0], workflow["id"], repetitions=2)
    try:
        _eventually(
            lambda: _read_report(api, run_id)["status"],
            lambda status: status == "running",
            description="the cancellable run to start",
        )
    finally:
        _response(api.post(f"/runs/{run_id}/cancel"))
    report = _wait_terminal(api, run_id)
    assert report["status"] == "cancelled"
    assert report["counts"]["cancelled"] == 2
    assert report["recommendation"]["status"] == "incomplete"
    _response(api.post(f"/runs/{run_id}/cancel"), expected=409)


def test_clickhouse_forwarding(live_reports):
    if not CLICKHOUSE_URL:
        pytest.skip("ClickHouse is not configured.")
    assert urlsplit(CLICKHOUSE_URL).hostname == "127.0.0.1", "Use a local ClickHouse for integration tests."
    run_id = live_reports["Sample form"]["run_id"]

    def forwarded():
        response = httpx.get(
            CLICKHOUSE_URL,
            params={
                "query": "SELECT DISTINCT kind FROM abrl.events WHERE run_id = {run_id:String} FORMAT TSV",
                "param_run_id": run_id,
            },
            auth=("abrl", CLICKHOUSE_PASSWORD),
            timeout=5,
            trust_env=False,
        )
        return set(_response(response).text.splitlines())

    _eventually(
        forwarded,
        lambda events: {"run_queued", "trial_completed", "run_completed"} <= events,
        description="the run events to reach ClickHouse",
    )


def test_finished_report_archives_without_workflow_inputs(api, live_reports):
    from abrl.artifacts import client

    s3 = client()
    if s3 is None:
        pytest.skip("S3 artifact storage is not configured.")
    original = live_reports["Sample form"]
    stored = _response(api.post(f"/runs/{original['run_id']}/archive")).json()
    assert stored["mode"] == "live"
    obj = s3.get_object(Bucket=stored["bucket"], Key=stored["key"])
    with obj["Body"] as body:
        archived = json.load(body)
    assert archived["config_sha256"] == original["config_sha256"]
    assert archived["completion"] == original["completion"]
    assert archived["trials"][0]["id"] == original["trials"][0]["id"]
    assert "snapshot" not in archived and "environment" not in archived["trials"][0]
