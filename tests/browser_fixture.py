"""Loopback browser fixtures for integration tests and UI smoke checks."""

import re
import secrets
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from abrl.diagnostic_fixture import router as diagnostic_router

router = APIRouter(include_in_schema=False)
router.include_router(diagnostic_router)
_MAX_BODY = 20_000

_CSS = """
:root { color-scheme: dark; font: 16px/1.6 system-ui, sans-serif; background: #0b1018; color: #e8edf5 }
body { max-width: 690px; margin: 12vh auto; padding: 0 24px }
nav { display: flex; flex-wrap: wrap; gap: 16px; border-bottom: 1px solid #24354b; padding-bottom: 16px }
a { color: #8bdaff } h1 { letter-spacing: -.035em; margin-bottom: 8px }
label { display: block; margin: 12px 0 }
input, select, button { border: 1px solid #48627a; border-radius: 6px; color: inherit; font: inherit; padding: 6px 10px; background: #152235 }
button { background: #a2e1ff; color: #101926; font-weight: 700; cursor: pointer }
"""


def _page(title: str, body: str, *, status: int = 200) -> HTMLResponse:
    nonce = secrets.token_urlsafe(18)
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en">
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · ABRL Lab</title><style nonce="{nonce}">{_CSS}</style>
<nav aria-label="Lab"><a href="/lab">ABRL Lab</a><a href="/lab/interaction">Interaction</a>
<a href="/lab/form" id="form-link">Form</a><a href="/lab/next" id="next-link">Journey</a>
<a href="/lab/repeat" id="repeat-link">Repeat visit</a></nav>
<main><h1>{escape(title)}</h1>{body}</main>
</html>""",
        status_code=status,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                "default-src 'none'; "
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
        "<p>Practice an interaction, submit a sample form, follow a two-page journey, or count repeat visits.</p>",
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
def lab_repeat(request: Request, persistent: bool = False) -> HTMLResponse:
    cookie = "abrl_profile_visit" if persistent else "abrl_lab_visit"
    previous = request.cookies.get(cookie, "")
    count = min(int(previous), 9998) + 1 if re.fullmatch(r"[0-9]{1,4}", previous) else 1
    response = _page(
        "Repeat visit", f'<p>Visits in this browser session: <output id="visit-count">{count}</output></p>'
    )
    response.set_cookie(
        cookie, str(count), max_age=300 if persistent else None,
        httponly=True, samesite="strict", path="/lab/repeat",
    )
    return response


def create_app():
    from fastapi import FastAPI
    from fixture_seed import seed_starter

    from abrl.api import app as operational_api
    from abrl.api import authenticate

    fixture_app = FastAPI()
    fixture_app.include_router(router)
    fixture_app.post("/test/seed", include_in_schema=False, dependencies=[Depends(authenticate)])(seed_starter)

    @fixture_app.middleware("http")
    async def enable_fixture(request, call_next):
        request.scope["abrl.test_fixture"] = True
        return await call_next(request)

    fixture_app.mount("/", operational_api)
    return fixture_app
