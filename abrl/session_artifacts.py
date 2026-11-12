"""Encrypted session artifacts, project access checks and bounded retention."""

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from abrl.access import authorize, scoped
from abrl.config import ROOT
from abrl.db import Session, new_id, now
from abrl.platform_models import BrowserProfile, BrowserSession, SessionArtifact
from abrl.platform_service import ACTIVE, event, utc_after

router = APIRouter(prefix="/api/v1")
ARTIFACT_ROOT = ROOT / ".abrl" / "artifacts"


def key():
    text = os.getenv("ABRL_ARTIFACT_ENCRYPTION_KEY") or os.environ["ABRL_PROFILE_ENCRYPTION_KEY"]
    return AESGCM(base64.urlsafe_b64decode(text))


def storage():
    if not os.getenv("ABRL_PROFILE_STORE_URL"):
        return None
    from abrl.session_runner import profile_s3

    return profile_s3()


def store_artifact(work, payload):
    content = base64.b64decode(payload["data_base64"], validate=True)
    if len(content) > 8 * 1024 * 1024:
        raise ValueError("Artifact exceeds 8MiB budget")
    media = payload.get("media_type")
    if media not in ("image/png", "image/jpeg", "application/json", "text/plain"):
        raise ValueError("Unsupported artifact media type")
    identity = new_id()
    object_key = f"session-artifacts/{work.id}/{identity}.aes"
    nonce = os.urandom(12)
    envelope = nonce + key().encrypt(nonce, content, object_key.encode())
    s3 = storage()
    path = ARTIFACT_ROOT / object_key
    written = False
    try:
        with Session.begin() as db:
            row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
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
            # Hold the ownership row through publication; a reaper skips this bounded write.
            if s3:
                s3.put_object(
                    Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"),
                    Key=object_key,
                    Body=envelope,
                    ContentType="application/octet-stream",
                )
            else:
                path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                with path.open("xb") as stream:
                    stream.write(envelope)
                path.chmod(0o600)
            written = True
            artifact = SessionArtifact(
                id=identity,
                project_id=row.project_id,
                session_id=row.id,
                name=Path(payload.get("name", "artifact")).name[:100],
                media_type=media,
                key=object_key,
                checksum=hashlib.sha256(envelope).hexdigest(),
                size=len(content),
                expires_at=utc_after(work.config.get("artifact_retention_days", 30) * 86400),
            )
            db.add(artifact)
            event(
                db,
                row.project_id,
                "artifact_saved",
                row.id,
                artifact_id=identity,
                media_type=media,
                size=len(content),
            )
    except BaseException:
        if written:
            if s3:
                s3.delete_object(Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"), Key=object_key)
            else:
                path.unlink(missing_ok=True)
        raise

    return identity


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
        if row.deleted_at or row.expires_at <= now():
            raise HTTPException(410, "Artifact retention has expired")
        s3 = storage()
        envelope = (
            s3.get_object(Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"), Key=row.key)[
                "Body"
            ].read(9 * 1024 * 1024)
            if s3
            else (ARTIFACT_ROOT / row.key).read_bytes()
        )
        if hashlib.sha256(envelope).hexdigest() != row.checksum:
            raise HTTPException(503, "Artifact checksum does not match")
        content = key().decrypt(envelope[:12], envelope[12:], row.key.encode())
        return Response(
            content,
            media_type=row.media_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": f'attachment; filename="artifact-{row.id}"',
            },
        )


def expire_artifacts():
    with Session.begin() as db:
        for row in db.scalars(
            select(SessionArtifact)
            .where(SessionArtifact.deleted_at.is_(None), SessionArtifact.expires_at < now())
            .limit(50)
            .with_for_update(skip_locked=True)
        ):
            s3 = storage()
            if s3:
                s3.delete_object(Bucket=os.getenv("ABRL_ARTIFACT_BUCKET", "abrl-artifacts"), Key=row.key)
            else:
                (ARTIFACT_ROOT / row.key).unlink(missing_ok=True)
            row.deleted_at = now()
            event(db, row.project_id, "artifact_expired", row.session_id, artifact_id=row.id)
