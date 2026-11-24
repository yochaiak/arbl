import base64
import io
import zipfile

import pytest
from sqlalchemy import select
from test_platform import client, create
from test_platform import db as db
from test_session_recordings import finish_recording, start_recording

from abrl import access, session_artifacts
from abrl.platform_models import BrowserSession, Membership, Project, SessionArtifact


@pytest.fixture
def artifact_session(db, monkeypatch, tmp_path):
    monkeypatch.setattr(session_artifacts, "Session", db)
    monkeypatch.setattr(session_artifacts, "ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(session_artifacts, "storage", lambda **_: None)
    monkeypatch.setenv("ABRL_ARTIFACT_ENCRYPTION_KEY", base64.urlsafe_b64encode(b"j" * 32).decode())
    batch = create(db, mode="live")
    with db() as connection:
        return connection.scalar(select(BrowserSession.id).where(BrowserSession.batch_id == batch.id))


def javascript_zip(source):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("sources/fixture.js", source)
    return output.getvalue()


def test_javascript_artifacts_download_per_attempt_with_project_access_and_retention(db, artifact_session):
    first = start_recording(db, artifact_session)
    first_archive = javascript_zip(b"window.firstAttempt = true;")
    first_id = session_artifacts.store_javascript_artifact(first, first_archive)
    assert session_artifacts.store_javascript_artifact(first, first_archive) == first_id
    finish_recording(db, first)

    second = start_recording(db, artifact_session)
    with db.begin() as connection:
        connection.get(BrowserSession, second.id).status = "stopping"
        user = access.ensure_bootstrap(connection)
        connection.get(Membership, ("default", user.id)).role = "viewer"
        connection.add(Project(id="other", name="Other project"))
        connection.add(Membership(project_id="other", user_id=user.id, role="owner"))
    second_archive = javascript_zip(b"window.cancelledAttempt = true;")
    second_id = session_artifacts.store_javascript_artifact(second, second_archive)

    with db() as connection:
        first_key = connection.get(SessionArtifact, first_id).key
        ciphertext = (session_artifacts.ARTIFACT_ROOT / first_key).read_bytes()
        assert b"window.firstAttempt" not in ciphertext
        assert len(list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes"))) == 2

    with client() as api:
        session_path = f"/api/v1/sessions/{artifact_session}"
        artifacts = api.get(f"{session_path}/artifacts").json()
        assert [item["name"] for item in artifacts] == [
            "javascript-attempt-1.zip",
            "javascript-attempt-2.zip",
        ]
        assert api.get(f"{session_path}/artifacts", headers={"X-Project-ID": "other"}).status_code == 404
        for attempt, identity, body in [(1, first_id, first_archive), (2, second_id, second_archive)]:
            url = f"/api/v1/artifacts/{identity}"
            response = api.get(url)
            assert response.status_code == 200
            assert response.content == body
            assert response.headers["Content-Type"] == "application/zip"
            assert (
                response.headers["Content-Disposition"]
                == f'attachment; filename="javascript-attempt-{attempt}.zip"'
            )
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert api.get(url, headers={"X-Project-ID": "other"}).status_code == 404

        with db.begin() as connection:
            connection.get(SessionArtifact, first_id).expires_at = "2000-01-01T00:00:00+00:00"
        session_artifacts.expire_artifacts()
        assert api.get(f"/api/v1/artifacts/{first_id}").status_code == 410
        assert [item["id"] for item in api.get(f"{session_path}/artifacts").json()] == [second_id]
    assert not (session_artifacts.ARTIFACT_ROOT / first_key).exists()


def test_javascript_archive_upload_cannot_publish_after_ownership_loss(db, artifact_session, monkeypatch):
    work = start_recording(db, artifact_session)
    write = session_artifacts._write_artifact_object

    def write_then_lose_ownership(s3, key, envelope):
        write(s3, key, envelope)
        with db.begin() as connection:
            connection.get(BrowserSession, work.id).lease_until = "2000-01-01T00:00:00+00:00"

    monkeypatch.setattr(session_artifacts, "_write_artifact_object", write_then_lose_ownership)
    with pytest.raises(ValueError, match="inactive ownership epoch"):
        session_artifacts.store_javascript_artifact(work, javascript_zip(b"window.lateUpload = true;"))
    with db() as connection:
        assert connection.scalar(select(SessionArtifact)) is None
    assert not list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes"))


def test_javascript_archive_rejects_size_limit_and_cleans_up_failed_publication(
    db, artifact_session, monkeypatch
):
    work = start_recording(db, artifact_session)
    with pytest.raises(ValueError, match="8 MiB budget"):
        session_artifacts.store_javascript_artifact(work, bytes(session_artifacts.ARTIFACT_MAX_BYTES + 1))

    def metadata_unavailable(*args, **kwargs):
        raise OSError("Fixture metadata storage unavailable")

    monkeypatch.setattr(session_artifacts, "event", metadata_unavailable)
    with pytest.raises(OSError, match="metadata storage unavailable"):
        session_artifacts.store_javascript_artifact(work, javascript_zip(b"window.rolledBack = true;"))
    with db() as connection:
        assert connection.scalar(select(SessionArtifact)) is None
    assert not list(session_artifacts.ARTIFACT_ROOT.rglob("*.aes"))
