"""Server-only AdScore verification and immutable, attempt-bound observations."""

import hashlib
import hmac
import ipaddress
import os

from fastapi import APIRouter, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import Field
from sqlalchemy import select

from abrl.config import OPERATOR_TOKEN
from abrl.db import Session, now
from abrl.platform_models import BrowserSession, ProviderObservation, SessionBatch
from abrl.platform_service import ACTIVE, event
from abrl.schemas import Input
from abrl.verifier import verify_observation

router = APIRouter()


def signer():
    secret = os.getenv("ABRL_CORRELATION_SECRET") or OPERATOR_TOKEN
    if len(secret) < 32:
        raise ValueError("Configure a strong ABRL_CORRELATION_SECRET")
    return URLSafeTimedSerializer(secret, salt="abrl-instrumented-v1")


def context_token(session_id, attempt_id, epoch):
    return signer().dumps({"session_id": session_id, "attempt_id": attempt_id, "epoch": epoch})


def decode_context(token):
    try:
        return signer().loads(token, max_age=7200)
    except (BadSignature, ValueError):
        raise HTTPException(401, "Expired or invalid session context")


class ObservationInput(Input):
    context: str = Field(min_length=16, max_length=2048)
    signature: str | None = Field(default=None, max_length=16384)
    idempotency_key: str = Field(min_length=8, max_length=100)
    client_ip: str = Field(max_length=64)
    user_agent: str = Field(min_length=1, max_length=1024)


def _active_attempt(session, context):
    return (
        session is not None
        and session.mode == "live"
        and session.lease_token == context["attempt_id"]
        and session.epoch == context["epoch"]
        and session.status in ACTIVE
        and bool(session.lease_until and session.lease_until > now())
    )


def observation_view(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "session_id",
            "attempt_id",
            "verification_status",
            "verdict",
            "raw_code",
            "accepted",
            "source",
            "provider_version",
            "created_at",
        )
    }


async def ingest(data, *, source):
    context = decode_context(data.context)
    request_hash = hashlib.sha256(data.model_dump_json().encode()).hexdigest()
    try:
        address = ipaddress.ip_address(data.client_ip)
        if isinstance(address, ipaddress.IPv6Address):
            if address.scope_id is not None:
                raise ValueError
            address = address.ipv4_mapped or address
        if not address.is_global or address.is_multicast:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "A trusted public client IP is required")
    with Session() as db:
        session = db.get(BrowserSession, context["session_id"])
        if not session or session.mode != "live":
            raise HTTPException(409, "Observations require a live session")
        prior = db.scalar(
            select(ProviderObservation).where(
                ProviderObservation.session_id == session.id,
                ProviderObservation.idempotency_key == data.idempotency_key,
            )
        )
        if prior:
            if prior.request_hash and prior.request_hash != request_hash:
                raise HTTPException(409, "Idempotency key belongs to different observation")
            return observation_view(prior)
        if not _active_attempt(session, context):
            raise HTTPException(409, "Observation belongs to an inactive attempt")
        batch = db.get(SessionBatch, session.batch_id)
        if batch.config["target"]["mode"] != "instrumented":
            raise HTTPException(409, "Browser-only sessions cannot ingest detector observations")
        target = batch.config["target"]
    zone = target.get("adscore_zone_id") or os.getenv("ABRL_ADSCORE_ZONE_ID", "")
    result = (
        await verify_observation(
            signature=data.signature, ip_address=str(address), user_agent=data.user_agent, zone_id=zone
        )
        if data.signature
        else {"verification_status": "unavailable", "verdict": None, "raw_code": None, "reason": "no_signature"}
    )
    with Session.begin() as db:
        session = db.scalar(
            select(BrowserSession).where(BrowserSession.id == context["session_id"]).with_for_update()
        )
        prior = db.scalar(
            select(ProviderObservation).where(
                ProviderObservation.session_id == session.id,
                ProviderObservation.idempotency_key == data.idempotency_key,
            )
        )
        if prior:
            if prior.request_hash and prior.request_hash != request_hash:
                raise HTTPException(409, "Idempotency key belongs to different observation")
            return observation_view(prior)
        if not _active_attempt(session, context):
            raise HTTPException(409, "Attempt finished during verification")
        existing = db.scalar(
            select(ProviderObservation).where(ProviderObservation.attempt_id == context["attempt_id"])
        )
        if existing:
            raise HTTPException(409, "An attempt already has an immutable provider observation")
        clean = result["verification_status"] == "verified" and result["raw_code"] == 0
        row = ProviderObservation(
            project_id=session.project_id,
            session_id=session.id,
            attempt_id=context["attempt_id"],
            idempotency_key=data.idempotency_key,
            verification_status=result["verification_status"],
            verdict=result["verdict"],
            raw_code=result["raw_code"],
            accepted=clean if target.get("acceptance_gate", "verified_clean") == "verified_clean" else True,
            source=source,
            provider_version=os.getenv("ABRL_ADSCORE_ALGORITHM", "v5_0101M"),
            request_hash=request_hash,
            observed_ip=str(address),
        )
        db.add(row)
        db.flush()
        event(
            db,
            session.project_id,
            "provider_observation",
            session.id,
            verification=row.verification_status,
            raw_code=row.raw_code,
            accepted=row.accepted,
        )
        return observation_view(row)


@router.post("/api/v1/integration-observations")
async def connector(data: ObservationInput, request: Request):
    """Authenticated site-backend connector. Never call this route from browser JavaScript."""
    secret = os.getenv("ABRL_OBSERVATION_CONNECTOR_TOKEN", "")
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if len(secret) < 32 or not hmac.compare_digest(secret.encode(), token.encode()):
        raise HTTPException(401, "Site connector authentication required")
    return await ingest(data, source="site_connector")


@router.get("/api/v1/sessions/{session_id}/observations")
def observations(session_id: str, request: Request):
    from abrl.access import authorize, scoped

    actor = authorize(request)
    with Session() as db:
        scoped(db, BrowserSession, session_id, actor)
        return [
            observation_view(r)
            for r in db.scalars(
                select(ProviderObservation)
                .where(ProviderObservation.session_id == session_id)
                .order_by(ProviderObservation.created_at)
            )
        ]
