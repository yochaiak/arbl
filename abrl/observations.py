"""Server-only AdScore verification and immutable, attempt-bound observations."""

import hashlib
import hmac
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import Field
from sqlalchemy import select

from abrl.config import OPERATOR_TOKEN
from abrl.db import Session, now
from abrl.lab import _bounded_body, _page, _request_metadata, verify_observation
from abrl.platform_models import BrowserSession, ProviderObservation, SessionBatch
from abrl.platform_service import ACTIVE, event
from abrl.schemas import Input

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
        raise HTTPException(401, "Expired or invalid research context")


class ObservationInput(Input):
    context: str = Field(min_length=16, max_length=2048)
    signature: str | None = Field(default=None, max_length=16384)
    idempotency_key: str = Field(min_length=8, max_length=100)
    client_ip: str = Field(max_length=64)
    user_agent: str = Field(min_length=1, max_length=1024)


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
        if not address.is_global or address.is_multicast:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "A trusted public client IP is required")
    with Session() as db:
        session = db.get(BrowserSession, context["session_id"])
        if not session or session.mode != "live":
            raise HTTPException(409, "Observations require a live research session")
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
        if (
            session.lease_token != context["attempt_id"]
            or session.epoch != context["epoch"]
            or session.status not in ACTIVE
            or not session.lease_until
            or session.lease_until <= now()
        ):
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
        else {"verification_status": "unavailable", "verdict": None, "raw_code": None}
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
        if (
            session.lease_token != context["attempt_id"]
            or session.epoch != context["epoch"]
            or session.status not in ACTIVE
            or not session.lease_until
            or session.lease_until <= now()
        ):
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


@router.get("/lab/research")
def research_page(request: Request, context: str, next: str = "/lab"):
    identity = decode_context(context)
    # Validate this request against the immutable authorized site, not a URL supplied by the browser.
    with Session() as db:
        session = db.get(BrowserSession, identity["session_id"])
        if not session or session.lease_token != identity["attempt_id"]:
            raise HTTPException(409, "Inactive research context")
        target = db.get(SessionBatch, session.batch_id).config["target"]
    if target["origin"] != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Research origin mismatch")
    if (
        urlsplit(next).path
        not in {"/lab", "/lab/form", "/lab/interaction", "/lab/next", "/lab/finish", "/lab/repeat"}
        or next.startswith("//")
        or "\\" in next
        or urlsplit(next).netloc
    ):
        raise HTTPException(422, "Invalid controlled workflow path")
    key = target.get("adscore_zone_key") or os.getenv("ABRL_ADSCORE_ZONE_KEY", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", key):
        raise HTTPException(503, "Configure a compatible AdScore test zone")
    payload = json.dumps(
        {"context": context, "idempotency_key": hashlib.sha256(context.encode()).hexdigest()}
    )
    script = f"""let sent=false;
async function save(signature){{if(sent)return;sent=true;try{{const response=await fetch('/lab/research',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{...{payload},signature}})}});if(!response.ok)throw Error();location.href={json.dumps(next).replace(chr(60), chr(92) + "u003c")};}}catch{{document.getElementById('research-status').textContent='Server verification unavailable';}}}}
const engine=document.createElement('script');engine.src='https://c.adsco.re/';engine.onload=()=>{{try{{AdscoreInit({json.dumps(key)},{{callback(result){{save(result?.error===0&&typeof result.signature==='string'?result.signature:null)}}}})}}catch{{save(null)}}}};engine.onerror=()=>save(null);document.head.appendChild(engine);setTimeout(()=>save(null),30000);"""
    response = _page(
        "Instrumented research",
        '<p id="research-status" role="status">Awaiting verified provider evidence</p>',
        script=script,
    )
    response.set_cookie(
        "abrl_research",
        context,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/lab",
        max_age=7200,
    )
    return response


@router.post("/lab/research")
async def research_callback(request: Request):
    expected = str(request.base_url).rstrip("/")
    if request.headers.get("origin") != expected:
        raise HTTPException(403, "Origin mismatch")
    ip, ua = _request_metadata(request)
    try:
        payload = json.loads(await _bounded_body(request))
    except ValueError:
        raise HTTPException(422, "Invalid research callback")
    if payload.get("context") != request.cookies.get("abrl_research"):
        raise HTTPException(403, "Research context mismatch")
    data = ObservationInput.model_validate({**payload, "client_ip": ip, "user_agent": ua})
    return await ingest(data, source="controlled_site")


def acceptance(request):
    token = request.cookies.get("abrl_research")
    if not token:
        return True
    context = decode_context(token)
    with Session() as db:
        session = db.get(BrowserSession, context["session_id"])
        if (
            not session
            or session.lease_token != context["attempt_id"]
            or session.status not in ACTIVE
            or not session.lease_until
            or session.lease_until <= now()
        ):
            return False
        observation = db.scalar(
            select(ProviderObservation).where(ProviderObservation.attempt_id == context["attempt_id"])
        )
        return bool(observation and observation.accepted)
