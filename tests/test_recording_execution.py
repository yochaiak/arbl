import base64
import subprocess

import pytest
from sqlalchemy import select
from test_browser import chromium as chromium
from test_browser import fixture as fixture
from test_platform import client, create
from test_platform import db as db

from abrl import platform_service, session_artifacts, session_runner
from abrl.db import TargetVersion, WorkflowVersion
from abrl.platform_models import BrowserSession, SessionAttempt, SessionEvent
from abrl.recording import recording_encoder_available


@pytest.mark.integration
async def test_local_session_runner_publishes_replay(chromium, fixture, db, monkeypatch, tmp_path):
    if not recording_encoder_available():
        pytest.skip("FFmpeg with libx264 is required")
    origin, visited, _ = fixture

    monkeypatch.setenv("ABRL_ARTIFACT_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"r" * 32).decode())
    monkeypatch.setattr(session_runner, "WORK_ROOT", tmp_path / "work")
    monkeypatch.setattr(session_artifacts, "Session", db)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(session_artifacts, "storage", lambda **_: None)
    monkeypatch.setattr(
        platform_service, "execution_capabilities", lambda: [{"id": "playwright", "available": True}]
    )
    with db.begin() as connection:
        target = connection.get(TargetVersion, ("target", 1))
        target.config = {**target.config, "origin": origin}
        workflow = connection.get(WorkflowVersion, ("workflow", 1))
        workflow.config = {
            **workflow.config,
            "steps": [
                {"action": "navigate", "url": "/lab", "post_navigation_wait_ms": 1700},
                {"action": "assert_visible", "selector": "#name"},
            ],
        }
    create(db, mode="live", artifact_policy="none", capture_javascript=False)
    with db.begin() as connection:
        session_runner.admission_tick(connection)
    work = session_runner.claim("standard")
    assert work is not None

    await session_runner.execute(work)

    with db() as connection:
        row = connection.get(BrowserSession, work.id)
        attempt = connection.get(SessionAttempt, work.lease)
        assert row.status == row.workflow_outcome == "completed", row.error
        assert attempt.recording_status == "ready"
        assert "recording_status" not in row.metadata_json
        assert not any("data_base64" in item for item in connection.scalars(select(SessionEvent.detail)))
    assert ("GET", "/lab", b"") in visited

    path = f"/api/v1/sessions/{work.id}"
    with client() as api:
        assert api.get(path).json()["recording_available"]
        assert api.get(f"{path}/artifacts").json() == []
        attempts = api.get(f"{path}/recordings").json()["attempts"]
        assert len(attempts) == 1 and attempts[0]["status"] == "ready"
        assert attempts[0]["segments"][0]["sequence"] == 0
        clip = api.get(f"{path}/recordings/1/segments/0")
        assert clip.status_code == 200, clip.text
        assert clip.headers["content-type"] == "video/mp4"
    replay = tmp_path / "replay.mp4"
    replay.write_bytes(clip.content)
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(replay), "-f", "null", "-"],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode()
