import base64
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_browser import https_asset_site as https_asset_site
from test_browser import never_cancel
from test_platform import client, data
from test_platform import db as db
from test_remote_browser import runtime

from abrl import browser, platform_service, session_artifacts, session_runner
from abrl.adapters import AdapterUnavailable
from abrl.browser import execute_workflow
from abrl.db import TargetVersion, WorkflowVersion
from abrl.javascript_capture import JavaScriptCapture
from abrl.platform_models import BrowserSession
from abrl.recording import SessionRecorder


@pytest.mark.integration
@pytest.mark.parametrize(
    "scenario", ["defaults", "completed", "failed", "cancelled", "launch_error", "storage_error", "disabled"]
)
async def test_runner_finalizes_javascript_capture(chromium, fixture, db, monkeypatch, tmp_path, scenario):
    origin, visited, _ = fixture
    monkeypatch.setenv("ABRL_ARTIFACT_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"j" * 32).decode())
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(session_runner, "STOPPING", False)
    monkeypatch.setattr(SessionRecorder, "start", lambda *_: None)
    monkeypatch.setattr(session_artifacts, "Session", db)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(session_artifacts, "storage", lambda **_: None)
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    if scenario == "storage_error":

        def fail_storage(*_):
            raise OSError("Private storage details")

        monkeypatch.setattr(session_runner, "store_javascript_artifact", fail_storage)
    if scenario == "launch_error":

        def fail_launch(*_, **__):
            raise AdapterUnavailable("Fixture adapter did not start", "unavailable")

        monkeypatch.setattr(browser, "launch_adapter", fail_launch)
    if scenario == "cancelled":
        record_activity = session_runner.record_activity

        def stop_after_navigation(work, activity):
            record_activity(work, activity)
            if activity["kind"] == "workflow_step" and activity["step"] == 2:
                session_runner.STOPPING = True

        monkeypatch.setattr(session_runner, "record_activity", stop_after_navigation)

    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": origin}
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {
            **workflow.config,
            "steps": [
                {"action": "navigate", "url": "/lab?console"},
                {
                    "action": "assert_visible",
                    "selector": "#missing" if scenario in {"failed", "cancelled"} else "#name",
                    "timeout_ms": 1200,
                },
            ],
        }
    with client() as api:
        launch = data(mode="live").model_dump(mode="json", exclude={"capture_javascript", "artifact_policy"})
        if scenario != "defaults":
            launch.update(artifact_policy="none", capture_javascript=scenario != "disabled")
        response = api.post("/api/v1/batches", json=launch)
        assert response.status_code == 201, response.text
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None

    await session_runner.execute(work)

    expected_outcome = {"failed": "failed", "cancelled": "cancelled", "launch_error": "failed"}.get(
        scenario, "completed"
    )
    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        assert row.status == row.workflow_outcome == expected_outcome, row.error
        capture = row.metadata_json.get("javascript_capture")
    assert visited.count(("GET", "/lab?console", b"")) == (0 if scenario == "launch_error" else 1)
    with client() as api:
        retained = api.get(f"/api/v1/sessions/{work.id}/artifacts")
        assert retained.status_code == 200, retained.text
        if scenario == "disabled":
            assert capture is None and retained.json() == []
            return
        if scenario == "storage_error":
            assert capture["status"] == "unavailable"
            assert capture["reason"] == "storage_unavailable"
            assert "artifact_id" not in capture and retained.json() == []
            return
        artifacts = {item["name"]: item for item in retained.json()}
        artifact = artifacts.pop("javascript-attempt-1.zip")
        if scenario == "defaults":
            assert artifacts.keys() == {"final.png", "console.json"}
            screenshot = api.get(f"/api/v1/artifacts/{artifacts['final.png']['id']}")
            assert screenshot.status_code == 200
            assert screenshot.content.startswith(b"\x89PNG\r\n\x1a\n")
            console = api.get(f"/api/v1/artifacts/{artifacts['console.json']['id']}")
            assert console.status_code == 200
            assert any(item["text"] == "private-filled-value" for item in console.json())
        else:
            assert not artifacts
        assert capture["status"] in (
            {"unavailable"} if scenario == "launch_error" else {"ready", "partial"}
        )
        assert capture["artifact_id"] == artifact["id"]
        assert artifact["name"] == "javascript-attempt-1.zip"
        response = api.get(f"/api/v1/artifacts/{artifact['id']}")
        assert response.status_code == 200, response.text
        assert "javascript-attempt-1.zip" in response.headers["content-disposition"]
    with ZipFile(BytesIO(response.content)) as archive:
        preview = json.loads(archive.read("manifest.json"))
        assert work.lease not in json.dumps(preview)
        if scenario == "launch_error":
            assert archive.namelist() == ["manifest.json"]
            return
        assert any(
            archive.read(name) == b"console.log('private-filled-value');"
            for name in archive.namelist()
            if name.startswith("sources/")
        )


@pytest.mark.integration
async def test_remote_capture_keeps_browser_delivered_cross_origin_module(chromium, https_asset_site):
    visited, _ = https_asset_site
    origin = "https://shop.example"
    capture = JavaScriptCapture()
    async with runtime(origin) as (connection, _):
        result = await execute_workflow(
            origin=origin,
            resource_origins=[],
            cancel=never_cancel,
            remote=connection,
            steps=[
                {"action": "navigate", "url": "/"},
                {"action": "assert_text", "selector": "#module", "value": "Loaded"},
                {"action": "assert_text", "selector": "#api", "value": "Blocked"},
            ],
            javascript_capture=capture,
        )
    assert result.outcome == "completed", result.error
    assert result.metadata["runtime_stopped"]
    assert ("assets.example", "GET", "/module.js") in visited
    assert not {"/post", "/get"} & {path for _, _, path in visited}
    content, _ = capture.build_archive({})
    url = "https://assets.example/module.js"
    with ZipFile(BytesIO(content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        (delivered,) = [source for source in manifest["responses"] if source.get("url") == url]
        (executed,) = [source for source in manifest["runtime_scripts"] if source.get("url") == url]
        assert executed["execution"] == "executed"
        assert archive.read(delivered["source"]) == archive.read(executed["source"])
        assert (
            archive.read(delivered["source"]) == b"document.querySelector('#module').textContent='Loaded';"
        )
