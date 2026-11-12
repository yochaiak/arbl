"""Deterministic local scenarios and opt-in manual AdScore diagnostics."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from html import escape
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .config import OPERATOR_TOKEN
from .db import ManualObservation, Session

router = APIRouter(include_in_schema=False)
_MAX_BODY = 20_000
_MANUAL_TTL = 120
_VERDICTS = {"clean", "potentially_unwanted", "proxy", "bot", "unknown"}

_CSS = """
:root { color-scheme: dark; font: 16px/1.6 system-ui, sans-serif; background: #0b1018; color: #e8edf5 }
body { max-width: 690px; margin: 12vh auto; padding: 0 24px }
nav { display: flex; flex-wrap: wrap; gap: 16px; border-bottom: 1px solid #24354b; padding-bottom: 16px }
a { color: #8bdaff } h1 { letter-spacing: -.035em; margin-bottom: 8px }
label { display: block; margin: 12px 0 }
input, select, button { border: 1px solid #48627a; border-radius: 6px; color: inherit; font: inherit; padding: 6px 10px; background: #152235 }
button { background: #a2e1ff; color: #101926; font-weight: 700; cursor: pointer }
"""


def _page(title: str, body: str, *, script: str = "", status: int = 200) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    script_tag = f'<script nonce="{nonce}">{script}</script>' if script else ""
    scripts = f"script-src 'nonce-{nonce}' https://*.adsco.re" if script else "script-src 'none'"
    connect = "connect-src 'self' https://*.adsco.re" if script else "connect-src 'none'"
    media = "img-src https://*.adsco.re data:; frame-src https://*.adsco.re;" if script else ""
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · ABRL Lab</title><style nonce="{nonce}">{_CSS}</style>
<nav aria-label="Lab"><a href="/lab">ABRL Lab</a><a href="/lab/interaction">Interaction</a>
<a href="/lab/form" id="form-link">Form</a><a href="/lab/next" id="next-link">Journey</a>
<a href="/lab/repeat" id="repeat-link">Repeat visit</a></nav>
<main><h1>{escape(title)}</h1>{body}</main>
{script_tag}
</html>""",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; {scripts}; {connect}; {media} "
                f"style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
        },
    )


async def _bounded_body(request: Request) -> bytes:
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > _MAX_BODY:
            raise HTTPException(status_code=413, detail="Request body too large")
        buffer.extend(chunk)
    return bytes(buffer)


async def _read_form(request: Request) -> dict[str, list[str]]:
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Expected a form submission")
    try:
        return parse_qs((await _bounded_body(request)).decode(), max_num_fields=5)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid form") from exc


@router.get("/lab")
def lab_home() -> HTMLResponse:
    return _page(
        "Controlled test site",
        '<p id="lab-ready">Lab ready</p>'
        "<p>Practice an interaction, submit a sample form, follow a two-page journey, or count repeat visits.</p>"
        '<p><a id="manual-link" href="/lab/manual">Manual AdScore diagnostics</a></p>',
    )


@router.get("/lab/interaction")
def lab_interaction() -> HTMLResponse:
    return _page(
        "Interaction",
        '<details><summary id="expand">Show test message</summary>'
        '<p id="interaction-complete">Interaction complete</p></details>',
    )


@router.get("/lab/form")
def lab_form() -> HTMLResponse:
    return _page(
        "Sample form",
        """<form method="post" action="/lab/form" autocomplete="off">
<label for="sample-name">Sample name</label><input id="sample-name" name="name" maxlength="80" required>
<label for="sample-topic">Topic</label><select id="sample-topic" name="topic">
<option value="research">Research</option><option value="testing">Testing</option></select>
<p><button type="submit" id="sample-submit">Submit sample</button></p></form>""",
    )


@router.post("/lab/form")
async def lab_form_submit(request: Request) -> HTMLResponse:
    from abrl.observations import acceptance
    if not acceptance(request):
        return _page("Action declined", '<p id="form-denied">The acceptance gate requires verified Clean evidence.</p>', status=403)
    form = await _read_form(request)
    name = form.get("name", [""])[0]
    topic = form.get("topic", [""])[0]
    if not 1 <= len(name.strip()) <= 80 or topic not in {"research", "testing"}:
        return _page(
            "Invalid sample", '<p id="form-error">Enter a sample name and choose a topic.</p>', status=422
        )
    return _page("Sample form", '<p id="form-success">Sample received</p>')


@router.get("/lab/next")
def lab_next() -> HTMLResponse:
    return _page(
        "Journey step one",
        '<p id="next-ready">First step ready</p><a id="finish-link" href="/lab/finish">Continue</a>',
    )


@router.get("/lab/finish")
def lab_finish() -> HTMLResponse:
    return _page("Journey step two", '<p id="journey-complete">Journey complete</p>')


@router.get("/lab/repeat")
def lab_repeat(request: Request) -> HTMLResponse:
    previous = request.cookies.get("abrl_lab_visit", "")
    count = min(int(previous), 9998) + 1 if re.fullmatch(r"[0-9]{1,4}", previous) else 1
    response = _page(
        "Repeat visit", f'<p>Visits in this browser session: <output id="visit-count">{count}</output></p>'
    )
    response.set_cookie("abrl_lab_visit", str(count), httponly=True, samesite="strict", path="/lab/repeat")
    return response


def _verifier_config() -> tuple[str, str] | None:
    url = os.getenv("ABRL_VERIFIER_URL", "http://127.0.0.1:8001").rstrip("/")
    token = os.getenv("ABRL_VERIFIER_TOKEN", "")
    try:
        parts = urlsplit(url)
        if (
            parts.scheme == "http"
            and parts.hostname == "127.0.0.1"
            and parts.port is not None
            and not any((parts.username, parts.password, parts.path, parts.query, parts.fragment))
            and len(token) >= 32
        ):
            return url, token
    except ValueError:
        pass
    return None


def _unavailable(reason: str) -> dict:
    return {"verification_status": "unavailable", "verdict": None, "raw_code": None, "reason": reason}


async def verify_observation(*, signature: str, ip_address: str, user_agent: str, zone_id: str) -> dict:
    config = _verifier_config()
    if config is None:
        return _unavailable("missing_configuration")
    url, token = config
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.post(
                f"{url}/verify",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "signature": signature,
                    "ip_address": ip_address,
                    "user_agent": user_agent,
                    "zone_id": zone_id,
                },
            )
        if response.status_code != 200:
            return _unavailable("verifier_unavailable")
        result = response.json()
        if not isinstance(result, dict):
            return _unavailable("invalid_verifier_response")
        status = result.get("verification_status")
        if (
            status == "verified"
            and result.get("verdict") in _VERDICTS
            and type(result.get("raw_code")) is int
        ):
            return {
                "verification_status": "verified",
                "verdict": result["verdict"],
                "raw_code": result["raw_code"],
                "reason": None,
            }
        if status == "failed":
            return {
                "verification_status": "failed",
                "verdict": None,
                "raw_code": None,
                "reason": "signature_verification_failed",
            }
    except (httpx.HTTPError, ValueError, TypeError):
        return _unavailable("verifier_unavailable")
    return _unavailable("invalid_verifier_response")


async def diagnostics_status() -> dict:
    config = _verifier_config()
    if config is None:
        return {"state": "not_configured", "reason": "missing_configuration", "last_verified_at": None}
    url, token = config
    try:
        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
            response = await client.get(f"{url}/health", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()
        if data.get("state") in {"not_configured", "pending_smoke_test", "ready"}:
            return {key: data.get(key) for key in ("state", "reason", "last_verified_at")}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        pass
    return {"state": "unavailable", "reason": "verifier_unavailable", "last_verified_at": None}


@dataclass(frozen=True)
class _ManualCheck:
    deadline: float
    ip: str
    user_agent: str


_pending: dict[str, _ManualCheck] = {}
_next_manual_start = 0.0


def _manual_config() -> tuple[str, str, str] | None:
    origin = os.getenv("ABRL_MANUAL_ORIGIN", "").rstrip("/")
    zone_key = os.getenv("ABRL_ADSCORE_ZONE_KEY", "")
    zone_id = os.getenv("ABRL_ADSCORE_ZONE_ID", "")
    try:
        parts = urlsplit(origin)
        if (
            parts.scheme == "https"
            and parts.hostname
            and not any((parts.username, parts.password, parts.path, parts.query, parts.fragment))
            and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", zone_key)
            and re.fullmatch(r"[1-9][0-9]{0,19}", zone_id)
            and _verifier_config() is not None
        ):
            return origin, zone_key, zone_id
    except ValueError:
        pass
    return None


def _request_metadata(request: Request) -> tuple[str, str]:
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast:
            raise ValueError
        user_agent = request.headers.get("user-agent", "")
        if not user_agent or len(user_agent) > 1024:
            raise ValueError
    except ValueError as exc:
        raise HTTPException(
            status_code=409, detail="A trusted public client address and User-Agent are required"
        ) from exc
    return str(address), user_agent


def _check_origin(request: Request, origin: str) -> None:
    if f"{request.url.scheme}://{request.url.netloc}" != origin or request.headers.get("origin") != origin:
        raise HTTPException(status_code=403, detail="Request origin mismatch")


@router.get("/lab/manual")
def manual_home(request: Request) -> HTMLResponse:
    config = _manual_config()
    if config is None or f"{request.url.scheme}://{request.url.netloc}" != config[0]:
        return _page(
            "Manual AdScore diagnostics",
            '<p id="manual-status">Awaiting an authorized HTTPS origin and AdScore verifier configuration.</p>',
        )
    return _page(
        "Manual AdScore diagnostics",
        """<p>Run a single check in the browser you are using. AdScore receives your browser and network information.</p>
<form action="/lab/manual/start" method="post">
<label for="operator-token">Operator token</label>
<input id="operator-token" name="token" type="password" required autocomplete="off">
<label><input type="checkbox" name="consent" value="yes" required> Send this diagnostic to AdScore</label>
<p><button type="submit">Run diagnostic</button></p></form>""",
    )


@router.post("/lab/manual/start")
async def manual_start(request: Request) -> HTMLResponse:
    global _next_manual_start
    config = _manual_config()
    if config is None:
        raise HTTPException(status_code=503, detail="Manual diagnostics are not configured")
    origin, zone_key, _ = config
    _check_origin(request, origin)
    form = await _read_form(request)
    token = form.get("token", [""])[0]
    if not OPERATOR_TOKEN or not secrets.compare_digest(token.encode(), OPERATOR_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Operator token required")
    if form.get("consent") != ["yes"]:
        raise HTTPException(status_code=400, detail="Consent is required")
    ip, user_agent = _request_metadata(request)
    readiness = await diagnostics_status()
    if readiness["state"] not in {"pending_smoke_test", "ready"}:
        raise HTTPException(status_code=503, detail="The AdScore verifier is not configured or reachable")
    now = time.monotonic()
    for key in list(_pending):
        if _pending[key].deadline <= now:
            del _pending[key]
    if now < _next_manual_start or len(_pending) >= 32:
        raise HTTPException(status_code=429, detail="Wait before starting another diagnostic")
    _next_manual_start = now + 5
    check_id = secrets.token_urlsafe(32)
    _pending[check_id] = _ManualCheck(now + _MANUAL_TTL, ip, user_agent)

    script = f"""
    const status = document.getElementById("manual-status");
    let sent = false;
    async function save(signature) {{
      if (sent) return;
      sent = true;
      try {{
        const response = await fetch("/lab/manual/observation", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ check_id: {json.dumps(check_id)}, signature }}),
        }});
        if (!response.ok) throw new Error("Observation rejected");
        const result = await response.json();
        status.textContent = result.verification_status === "verified"
          ? "Diagnostic recorded. The signature was verified."
          : "Diagnostic recorded. A verified verdict was unavailable.";
      }} catch {{
        status.textContent = "The diagnostic could not be recorded.";
      }}
    }}
    const engine = document.createElement("script");
    engine.src = "https://c.adsco.re/";
    engine.onload = () => {{
      try {{
        AdscoreInit({json.dumps(zone_key)}, {{
          callback(result) {{
            const signature = result?.error === 0 && typeof result.signature === "string"
              ? result.signature : null;
            save(signature);
          }},
        }});
      }} catch {{
        save(null);
      }}
    }};
    engine.onerror = () => save(null);
    document.head.appendChild(engine);
    setTimeout(() => save(null), 30000);
    """
    return _page(
        "Manual AdScore diagnostics",
        '<p id="manual-status" role="status">Waiting for AdScore…</p>',
        script=script,
    )


def _persist_manual(result: dict) -> None:
    with Session.begin() as session:
        session.add(
            ManualObservation(
                verification_status=result["verification_status"],
                verdict=result["verdict"],
                raw_code=result["raw_code"],
            )
        )


@router.post("/lab/manual/observation")
async def manual_observation(request: Request) -> dict:
    config = _manual_config()
    if config is None:
        raise HTTPException(status_code=503, detail="Manual diagnostics are not configured")
    origin, _, zone_id = config
    _check_origin(request, origin)
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise HTTPException(status_code=415, detail="Expected a JSON observation")
    try:
        payload = json.loads(await _bounded_body(request))
    except (ValueError, RecursionError) as exc:
        raise HTTPException(status_code=400, detail="Invalid observation") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid observation")
    check_id, signature = payload.get("check_id"), payload.get("signature")
    if (
        not isinstance(check_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", check_id)
        or (signature is not None and (not isinstance(signature, str) or len(signature) > 16_384))
    ):
        raise HTTPException(status_code=400, detail="Invalid observation")
    ip, user_agent = _request_metadata(request)
    check = _pending.get(check_id)
    if (
        check is None
        or check.deadline <= time.monotonic()
        or (check.ip, check.user_agent) != (ip, user_agent)
    ):
        raise HTTPException(
            status_code=409, detail="The diagnostic session is missing, expired, or changed"
        )
    del _pending[check_id]

    result = (
        await verify_observation(signature=signature, ip_address=ip, user_agent=user_agent, zone_id=zone_id)
        if signature
        else _unavailable("provider_error")
    )
    await asyncio.to_thread(_persist_manual, result)
    return {"verification_status": result["verification_status"]}
