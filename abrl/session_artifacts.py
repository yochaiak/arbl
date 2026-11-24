"""Encrypted session artifacts, project access checks and bounded retention."""

import base64
import hashlib
import os
from collections import defaultdict
from contextlib import closing, suppress
from datetime import datetime, timedelta
from pathlib import Path

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func, select

from abrl.access import authorize, scoped
from abrl.config import ROOT
from abrl.db import Session, new_id, now
from abrl.platform_models import BrowserProfile, BrowserSession, SessionArtifact, SessionAttempt
from abrl.platform_service import ACTIVE, event, recording_status, utc_after
from abrl.platform_views import SessionRecordingManifest

router = APIRouter(prefix="/api/v1")
ARTIFACT_ROOT = ROOT / ".abrl" / "artifacts"
ARTIFACT_MAX_BYTES = 8 * 1024 * 1024
RECORDING_MAX_BYTES = 256 * 1024 * 1024
_JAVASCRIPT_MEDIA_TYPE = "application/zip"
_NONCE_BYTES = 12
_ENVELOPE_OVERHEAD = _NONCE_BYTES + 16  # AES-GCM authentication tag
_BOUNDED_STORAGE_CONFIG = Config(
    connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 1, "mode": "standard"}
)


def key():
    text = os.getenv("ABRL_ARTIFACT_ENCRYPTION_KEY") or os.environ["ABRL_PROFILE_ENCRYPTION_KEY"]
    return AESGCM(base64.urlsafe_b64decode(text))


def storage(*, bounded=False):
    if not os.getenv("ABRL_PROFILE_STORE_URL"):
        return None
    from abrl.session_runner import profile_s3

    return profile_s3(config=_BOUNDED_STORAGE_CONFIG) if bounded else profile_s3()


def _owned_session(db, work, *, lock=False):
    query = select(BrowserSession).where(BrowserSession.id == work.id)
    row = db.scalar(query.with_for_update() if lock else query)
    profile = db.get(BrowserProfile, work.profile_id)
    if (
        not row
        or row.lease_token != work.lease
        or row.epoch != work.epoch
        or row.status not in ACTIVE
        or not row.lease_until
        or row.lease_until <= now()
        or not profile
        or profile.owner_session_id != work.id
        or profile.epoch != work.epoch
    ):
        raise ValueError("Artifact belongs to an inactive ownership epoch")
    return row


def _artifact_envelope(content, object_key):
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + key().encrypt(nonce, content, object_key.encode())


def _write_artifact_object(s3, object_key, envelope):
    if s3:
        s3.put_object(
            Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"),
            Key=object_key,
            Body=envelope,
            ContentType="application/octet-stream",
        )
    else:
        path = ARTIFACT_ROOT / object_key
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        created = False
        try:
            with path.open("xb") as stream:
                created = True
                path.chmod(0o600)
                stream.write(envelope)
        except BaseException:
            if created:
                path.unlink(missing_ok=True)
            raise


def _remove_artifact_object(s3, object_key):
    if s3:
        s3.delete_object(Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"), Key=object_key)
    else:
        (ARTIFACT_ROOT / object_key).unlink(missing_ok=True)


def _prepare_artifact(work, content, media, name):
    identity = new_id()
    object_key = f"session-artifacts/{work.id}/{identity}.aes"
    envelope = _artifact_envelope(content, object_key)
    artifact = SessionArtifact(
        id=identity,
        session_id=work.id,
        name=name,
        media_type=media,
        key=object_key,
        checksum=hashlib.sha256(envelope).hexdigest(),
        size=len(content),
        expires_at=utc_after(work.config.get("artifact_retention_days", 30) * 86400),
    )
    return artifact, envelope


def _publish_artifact(db, row, artifact):
    artifact.project_id = row.project_id
    db.add(artifact)
    event(
        db,
        row.project_id,
        "artifact_saved",
        row.id,
        artifact_id=artifact.id,
        media_type=artifact.media_type,
        size=artifact.size,
    )


def store_artifact(work, payload):
    content = base64.b64decode(payload["data_base64"], validate=True)
    if len(content) > ARTIFACT_MAX_BYTES:
        raise ValueError("Artifact exceeds 8MiB budget")
    media = payload.get("media_type")
    if media not in ("image/png", "image/jpeg", "application/json", "text/plain"):
        raise ValueError("Unsupported artifact media type")
    name = Path(payload.get("name", "artifact")).name[:100]
    artifact, envelope = _prepare_artifact(work, content, media, name)
    s3 = storage()
    written = False
    try:
        with Session.begin() as db:
            row = _owned_session(db, work, lock=True)
            # Hold the ownership row through publication; a reaper skips this bounded write.
            _write_artifact_object(s3, artifact.key, envelope)
            written = True
            _publish_artifact(db, row, artifact)
    except BaseException:
        if written:
            _remove_artifact_object(s3, artifact.key)
        raise

    return artifact.id


def _active_live_attempt(db, work, *, lock=False):
    row = _owned_session(db, work, lock=lock)
    attempt = db.get(SessionAttempt, work.lease)
    if (
        row.mode != "live"
        or not attempt
        or attempt.session_id != row.id
        or attempt.epoch != row.epoch
        or attempt.finished_at is not None
    ):
        raise ValueError("Artifact belongs to an inactive live attempt")
    return row, attempt


def _find_javascript_artifact(db, work, name):
    return db.scalar(
        select(SessionArtifact.id).where(
            SessionArtifact.session_id == work.id,
            SessionArtifact.attempt_id.is_(None),
            SessionArtifact.name == name,
            SessionArtifact.media_type == _JAVASCRIPT_MEDIA_TYPE,
        )
    )


def store_javascript_artifact(work, archive: bytes) -> str:
    if not archive or len(archive) > ARTIFACT_MAX_BYTES:
        raise ValueError("JavaScript archive is empty or exceeds the 8 MiB budget")
    with Session() as db:
        _, attempt = _active_live_attempt(db, work)
        name = f"javascript-attempt-{attempt.number}.zip"
        existing = _find_javascript_artifact(db, work, name)
        if existing:
            return existing

    artifact, envelope = _prepare_artifact(work, archive, _JAVASCRIPT_MEDIA_TYPE, name)
    s3 = storage(bounded=True)
    written = published = False
    try:
        # Storage can finish after ownership changes. Fence publication under the session lock.
        _write_artifact_object(s3, artifact.key, envelope)
        written = True
        with Session.begin() as db:
            row, _ = _active_live_attempt(db, work, lock=True)
            existing = _find_javascript_artifact(db, work, name)
            if existing:
                return existing
            _publish_artifact(db, row, artifact)
        published = True
        return artifact.id
    finally:
        if not published and (written or s3 is not None):
            with suppress(BotoCoreError, ClientError, OSError):
                _remove_artifact_object(s3, artifact.key)


def store_recording_segment(work, segment):
    if (
        type(segment.sequence) is not int
        or type(segment.start_ms) is not int
        or type(segment.duration_ms) is not int
        or segment.sequence < 0
        or segment.start_ms < 0
        or segment.duration_ms <= 0
    ):
        raise ValueError("Invalid recording segment metadata")
    with segment.path.open("rb") as stream:
        content = stream.read(ARTIFACT_MAX_BYTES + 1)
    if not content or len(content) > ARTIFACT_MAX_BYTES:
        raise ValueError("Recording segment is empty or exceeds the 8 MiB budget")

    with Session() as db:
        _active_live_attempt(db, work)

    identity = new_id()
    object_key = f"session-artifacts/{work.id}/{identity}.aes"
    envelope = _artifact_envelope(content, object_key)
    s3 = storage(bounded=True)
    written = published = False
    try:
        # Upload outside the row lock so slow storage does not block workflow activity.
        _write_artifact_object(s3, object_key, envelope)
        written = True
        with Session.begin() as db:
            row, attempt = _active_live_attempt(db, work, lock=True)
            clips = select(SessionArtifact).where(SessionArtifact.attempt_id == attempt.id)
            existing = db.scalar(clips.where(SessionArtifact.video_sequence == segment.sequence))
            if existing:
                if (
                    existing.video_start_ms != segment.start_ms
                    or existing.video_duration_ms != segment.duration_ms
                ):
                    raise ValueError("Recording segment sequence already exists with different metadata")
                return existing.id
            total = db.scalar(
                select(func.coalesce(func.sum(SessionArtifact.size), 0)).where(
                    SessionArtifact.attempt_id == attempt.id
                )
            )
            if total + len(content) > RECORDING_MAX_BYTES:
                raise ValueError("Recording exceeds the 256 MiB attempt budget")
            expiry = datetime.fromisoformat(attempt.started_at) + timedelta(
                days=work.config.get("artifact_retention_days", 30)
            )
            db.add(
                SessionArtifact(
                    id=identity,
                    project_id=row.project_id,
                    session_id=row.id,
                    attempt_id=attempt.id,
                    name=f"recording-{attempt.number}-{segment.sequence}.mp4",
                    media_type="video/mp4",
                    key=object_key,
                    checksum=hashlib.sha256(envelope).hexdigest(),
                    size=len(content),
                    expires_at=expiry.isoformat(timespec="microseconds"),
                    video_sequence=segment.sequence,
                    video_start_ms=segment.start_ms,
                    video_duration_ms=segment.duration_ms,
                )
            )
        published = True
        return identity
    finally:
        if not published and (written or s3 is not None):
            _remove_artifact_object(s3, object_key)


def _read_artifact(row, *, bounded=False):
    if row.deleted_at or row.expires_at <= now():
        raise HTTPException(410, "Artifact retention has expired")
    s3 = storage(bounded=True) if bounded else storage()
    envelope_limit = ARTIFACT_MAX_BYTES + _ENVELOPE_OVERHEAD
    try:
        if s3:
            with closing(
                s3.get_object(Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"), Key=row.key)["Body"]
            ) as stream:
                envelope = stream.read(envelope_limit + 1)
        else:
            with (ARTIFACT_ROOT / row.key).open("rb") as stream:
                envelope = stream.read(envelope_limit + 1)
        if len(envelope) > envelope_limit or hashlib.sha256(envelope).hexdigest() != row.checksum:
            raise HTTPException(503, "Artifact checksum does not match")
        return key().decrypt(envelope[:_NONCE_BYTES], envelope[_NONCE_BYTES:], row.key.encode())
    except (BotoCoreError, ClientError, OSError, InvalidTag) as exc:
        if row.expires_at <= now():
            raise HTTPException(410, "Artifact retention has expired") from exc
        raise HTTPException(503, "Artifact is unavailable") from exc


@router.get("/sessions/{session_id}/artifacts")
def list_artifacts(session_id: str, request: Request):
    actor = authorize(request)
    with Session() as db:
        scoped(db, BrowserSession, session_id, actor)
        return [
            {
                k: getattr(r, k)
                for k in ("id", "name", "media_type", "size", "checksum", "created_at", "expires_at")
            }
            for r in db.scalars(
                select(SessionArtifact)
                .where(
                    SessionArtifact.session_id == session_id,
                    SessionArtifact.attempt_id.is_(None),
                    SessionArtifact.deleted_at.is_(None),
                    SessionArtifact.expires_at > now(),
                )
                .order_by(SessionArtifact.created_at)
            )
        ]


@router.get("/artifacts/{artifact_id}")
def download_artifact(artifact_id: str, request: Request):
    actor = authorize(request)
    with Session() as db:
        row = scoped(db, SessionArtifact, artifact_id, actor)
        if row.attempt_id is not None:
            raise HTTPException(404, "Artifact not found in this project")
        javascript = row.media_type == _JAVASCRIPT_MEDIA_TYPE
        filename = row.name if javascript else f"artifact-{row.id}"
        return Response(
            _read_artifact(row, bounded=javascript),
            media_type=row.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )


def _recording_clips(session):
    return select(SessionArtifact).where(
        SessionArtifact.session_id == session.id,
        SessionArtifact.project_id == session.project_id,
        SessionArtifact.attempt_id.is_not(None),
    )


@router.get("/sessions/{session_id}/recordings", response_model=SessionRecordingManifest)
def session_recordings(session_id: str, request: Request, response: Response):
    actor = authorize(request, "operator")
    response.headers["Cache-Control"] = "no-store"
    with Session() as db:
        session = scoped(db, BrowserSession, session_id, actor)
        attempts = db.scalars(
            select(SessionAttempt)
            .where(SessionAttempt.session_id == session.id)
            .order_by(SessionAttempt.number.desc())
        ).all()
        clips_by_attempt = defaultdict(list)
        for clip in db.scalars(_recording_clips(session).order_by(SessionArtifact.video_sequence)):
            clips_by_attempt[clip.attempt_id].append(clip)
        timestamp = now()
        manifest = []
        for attempt in attempts:
            clips = clips_by_attempt[attempt.id]
            retained = [clip for clip in clips if clip.deleted_at is None and clip.expires_at > timestamp]
            manifest.append(
                {
                    "attempt_number": attempt.number,
                    "status": recording_status(
                        attempt, has_segments=bool(clips), has_retained_segments=bool(retained)
                    ),
                    "segments": [
                        {
                            "sequence": clip.video_sequence,
                            "start_ms": clip.video_start_ms,
                            "duration_ms": clip.video_duration_ms,
                        }
                        for clip in retained
                        if attempt.finished_at is not None
                    ],
                }
            )
        return {"attempts": manifest}


@router.get(
    "/sessions/{session_id}/recordings/{attempt_number}/segments/{sequence}",
    responses={200: {"content": {"video/mp4": {"schema": {"type": "string", "format": "binary"}}}}},
    response_class=Response,
)
def recording_segment(session_id: str, attempt_number: int, sequence: int, request: Request):
    actor = authorize(request, "operator")
    with Session() as db:
        session = scoped(db, BrowserSession, session_id, actor)
        attempt = db.scalar(
            select(SessionAttempt).where(
                SessionAttempt.session_id == session.id,
                SessionAttempt.number == attempt_number,
                SessionAttempt.finished_at.is_not(None),
            )
        )
        if not attempt:
            raise HTTPException(404, "Finished recording attempt not found")
        clip = db.scalar(
            _recording_clips(session).where(
                SessionArtifact.attempt_id == attempt.id, SessionArtifact.video_sequence == sequence
            )
        )
        if not clip:
            raise HTTPException(404, "Recording segment not found")
        return Response(
            _read_artifact(clip, bounded=True),
            media_type="video/mp4",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )


def _expire_recording_clips(clips):
    if not clips:
        return
    s3 = storage(bounded=True)
    failed_keys = set()
    if s3:
        result = s3.delete_objects(
            Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"),
            Delete={"Objects": [{"Key": clip.key} for clip in clips], "Quiet": True},
        )
        failed_keys = {error["Key"] for error in result.get("Errors", [])}
    else:
        for clip in clips:
            _remove_artifact_object(None, clip.key)
    expired_at = now()
    for clip in clips:
        if clip.key not in failed_keys:
            clip.deleted_at = expired_at


def expire_artifacts():
    with Session.begin() as db:
        expired = db.scalars(
            select(SessionArtifact)
            .where(SessionArtifact.deleted_at.is_(None), SessionArtifact.expires_at < now())
            .limit(50)
            .with_for_update(skip_locked=True)
        ).all()
        _expire_recording_clips([row for row in expired if row.attempt_id is not None])
        for row in expired:
            if row.attempt_id is not None:
                continue
            s3 = storage(bounded=True) if row.media_type == _JAVASCRIPT_MEDIA_TYPE else storage()
            _remove_artifact_object(s3, row.key)
            row.deleted_at = now()
            event(db, row.project_id, "artifact_expired", row.session_id, artifact_id=row.id)
