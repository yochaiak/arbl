"""Attempt-scoped session recordings and authenticated playback."""

import base64
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_platform
from sqlalchemy import select

from abrl import access, session_artifacts, session_runner
from abrl.db import new_id, now
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Membership,
    Project,
    SessionArtifact,
    SessionAttempt,
    SessionEvent,
)
from abrl.platform_service import utc_after

db = test_platform.db
client = test_platform.client


@pytest.fixture
def recording_storage(db, monkeypatch, tmp_path):
    monkeypatch.setattr(session_artifacts, "Session", db)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(session_artifacts, "storage", lambda **_: None)
    monkeypatch.setenv("ABRL_ARTIFACT_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"r" * 32).decode())
    batch = test_platform.create(db, mode="live")
    with db() as connection:
        session_id = connection.scalar(select(BrowserSession.id).where(BrowserSession.batch_id == batch.id))
    return session_id


def start_recording(db, session_id):
    with db.begin() as connection:
        session = connection.get(BrowserSession, session_id)
        session.status = "running"
        session.finished_at = None
        session.lease_token = new_id()
        session.lease_until = utc_after(300)
        session.epoch += 1
        session.attempt_number += 1
        profile = connection.get(BrowserProfile, session.profile_id)
        profile.owner_session_id = session.id
        profile.epoch = session.epoch
        connection.add(
            SessionAttempt(
                id=session.lease_token,
                session_id=session.id,
                number=session.attempt_number,
                epoch=session.epoch,
                recording_status="recording",
            )
        )
        return SimpleNamespace(
            id=session.id,
            lease=session.lease_token,
            epoch=session.epoch,
            profile_id=session.profile_id,
            config={"artifact_retention_days": 30},
        )


def finish_recording(db, work, *, status="ready"):
    with db.begin() as connection:
        attempt = connection.get(SessionAttempt, work.lease)
        attempt.finished_at = now()
        attempt.recording_status = status
        row = connection.get(BrowserSession, work.id)
        row.finished_at = attempt.finished_at
        row.status = "completed"


def clip(tmp_path: Path, sequence=0, content=b"example mp4 data"):
    path = tmp_path / f"clip-{sequence}.mp4"
    path.write_bytes(content)
    return SimpleNamespace(path=path, sequence=sequence, start_ms=30_000 * sequence, duration_ms=30_000)


def test_recording_manifest_and_ordered_encrypted_playback(db, recording_storage, tmp_path):
    session_id = recording_storage
    with client() as api:
        url = f"/api/v1/sessions/{session_id}"
        historical = api.get(url).json()
        assert historical["recording_status"] == "unavailable"
        assert historical["recording_available"] is False

        work = start_recording(db, session_id)
        second = clip(tmp_path, 1, content=b"second clip")
        first = clip(tmp_path, content=b"first clip")
        second_id = session_artifacts.store_recording_segment(work, second)
        first_id = session_artifacts.store_recording_segment(work, first)
        assert session_artifacts.store_recording_segment(work, first) == first_id

        assert api.get(url).json()["recording_status"] == "recording"
        assert api.get(url).json()["recording_available"] is False
        assert api.get(url + "/recordings").json() == {
            "attempts": [{"attempt_number": 1, "status": "recording", "segments": []}]
        }
        segment_url = url + "/recordings/1/segments/0"
        assert api.get(segment_url).status_code == 404

        finish_recording(db, work)
        row = api.get("/api/v1/sessions", params={"limit": 1}).json()["items"][0]
        assert (row["recording_status"], row["recording_available"]) == ("ready", True)
        response = api.get(url + "/recordings")
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {
            "attempts": [
                {
                    "attempt_number": 1,
                    "status": "ready",
                    "segments": [
                        {"sequence": 0, "start_ms": 0, "duration_ms": 30_000},
                        {"sequence": 1, "start_ms": 30_000, "duration_ms": 30_000},
                    ],
                }
            ]
        }
        response = api.get(segment_url)
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["cache-control"] == "no-store"
        assert response.content == b"first clip"
        assert api.get(url + "/artifacts").json() == []
        assert api.get(f"/api/v1/artifacts/{first_id}").status_code == 404

    with db() as connection:
        first_row = connection.get(SessionArtifact, first_id)
        second_row = connection.get(SessionArtifact, second_id)
        assert first_row.expires_at == second_row.expires_at
        encrypted = (session_artifacts.ARTIFACT_ROOT / first_row.key).read_bytes()
        assert b"first clip" not in encrypted
    assert len(list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes"))) == 2


def test_interrupted_recording_remains_playable_during_retry_until_expiry(db, recording_storage, tmp_path):
    session_id = recording_storage
    work = start_recording(db, session_id)
    identity = session_artifacts.store_recording_segment(work, clip(tmp_path))
    finish_recording(db, work, status="recording")
    start_recording(db, session_id)

    with client() as api:
        url = f"/api/v1/sessions/{session_id}"
        current = api.get(url).json()
        assert (current["recording_status"], current["recording_available"]) == ("recording", True)
        assert api.get(url + "/recordings").json() == {
            "attempts": [
                {"attempt_number": 2, "status": "recording", "segments": []},
                {
                    "attempt_number": 1,
                    "status": "partial",
                    "segments": [{"sequence": 0, "start_ms": 0, "duration_ms": 30_000}],
                },
            ]
        }
        assert api.get(url + "/recordings/1/segments/0").status_code == 200

        with db.begin() as connection:
            connection.get(SessionArtifact, identity).expires_at = "2000-01-01T00:00:00+00:00"
        session_artifacts.expire_artifacts()
        assert api.get(url + "/recordings/1/segments/0").status_code == 410
        assert api.get(url + "/recordings").json()["attempts"][1] == {
            "attempt_number": 1, "status": "expired", "segments": [],
        }
        assert api.get(url).json()["recording_available"] is False
    assert list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes")) == []


@pytest.mark.parametrize(("role", "allowed"), [("viewer", False), ("analyst", False), ("operator", True)])
def test_recording_access_respects_project_role(db, recording_storage, tmp_path, role, allowed):
    session_id = recording_storage
    work = start_recording(db, session_id)
    session_artifacts.store_recording_segment(work, clip(tmp_path))
    finish_recording(db, work)
    with db.begin() as connection:
        user = access.ensure_bootstrap(connection)
        connection.get(Membership, ("default", user.id)).role = role
        connection.add(Project(id="other", name="Other project"))
        connection.add(Membership(project_id="other", user_id=user.id, role="owner"))

    with client() as api:
        url = f"/api/v1/sessions/{session_id}"
        row = api.get(url).json()
        assert row["recording_available"] is allowed
        assert row["recording_status"] == ("ready" if allowed else "unavailable")
        for endpoint in ("/recordings", "/recordings/1/segments/0"):
            assert api.get(url + endpoint).status_code == (200 if allowed else 403)
            assert api.get(url + endpoint, headers={"X-Project-ID": "other"}).status_code == 404


def test_recording_cannot_publish_after_losing_ownership_during_upload(db, recording_storage, tmp_path, monkeypatch):
    work = start_recording(db, recording_storage)
    original = session_artifacts._write_artifact_object

    def lose_lease_after_upload(s3, key, envelope):
        original(s3, key, envelope)
        with db.begin() as connection:
            connection.get(BrowserSession, work.id).lease_until = "2000-01-01T00:00:00+00:00"

    monkeypatch.setattr(session_artifacts, "_write_artifact_object", lose_lease_after_upload)
    with pytest.raises(ValueError, match="inactive ownership epoch"):
        session_artifacts.store_recording_segment(work, clip(tmp_path))
    with db() as connection:
        assert connection.scalars(select(SessionArtifact)).all() == []
    assert list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes")) == []


def test_segment_and_attempt_size_limits(db, recording_storage, tmp_path, monkeypatch):
    work = start_recording(db, recording_storage)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_MAX_BYTES", 16)
    monkeypatch.setattr(session_artifacts, "RECORDING_MAX_BYTES", 20)
    with pytest.raises(ValueError, match="empty or exceeds"):
        session_artifacts.store_recording_segment(work, clip(tmp_path, content=b"X" * 17))
    session_artifacts.store_recording_segment(work, clip(tmp_path, content=b"X" * 11))
    with pytest.raises(ValueError, match="attempt budget"):
        session_artifacts.store_recording_segment(work, clip(tmp_path, sequence=1, content=b"X" * 10))
    assert len(list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes"))) == 1


def test_only_video_requests_use_the_bounded_object_storage_configuration(monkeypatch):
    configurations = []

    def client(*, config=None):
        configurations.append(config)
        return object()

    monkeypatch.setenv("ABRL_PROFILE_STORE_URL", "https://example.invalid")
    monkeypatch.setattr(session_runner, "profile_s3", client)
    session_artifacts.storage()
    session_artifacts.storage(bounded=True)
    normal, recording = configurations
    assert normal is None
    assert recording.connect_timeout == 2
    assert recording.read_timeout == 4
    assert recording.retries["total_max_attempts"] == 1


def test_recording_expiry_batches_deletes_and_preserves_failed_keys_for_retry(
    db, recording_storage, tmp_path, monkeypatch
):
    work = start_recording(db, recording_storage)
    first_id = session_artifacts.store_recording_segment(work, clip(tmp_path))
    second_id = session_artifacts.store_recording_segment(work, clip(tmp_path, sequence=1))
    with db.begin() as connection:
        for identity in (first_id, second_id):
            connection.get(SessionArtifact, identity).expires_at = "2000-01-01T00:00:00+00:00"
        first_key = connection.get(SessionArtifact, first_id).key
        second_key = connection.get(SessionArtifact, second_id).key

    deletes = []

    def delete_objects(*, Bucket, Delete):
        deletes.append(Delete)
        return {"Errors": [{"Key": second_key}]}

    def bounded_storage(*, bounded):
        assert bounded
        return SimpleNamespace(delete_objects=delete_objects)

    monkeypatch.setattr(session_artifacts, "storage", bounded_storage)
    session_artifacts.expire_artifacts()
    assert len(deletes) == 1
    assert {item["Key"] for item in deletes[0]["Objects"]} == {first_key, second_key}
    with db() as connection:
        assert connection.get(SessionArtifact, first_id).deleted_at is not None
        assert connection.get(SessionArtifact, second_id).deleted_at is None
        assert connection.scalar(select(SessionEvent).where(SessionEvent.kind == "artifact_expired")) is None
