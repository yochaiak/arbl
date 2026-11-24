"""OIDC code flow with server-validated issuer/nonce and signed, secure session cookies."""

import os
import secrets

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from abrl.config import OPERATOR_TOKEN, WEB_ORIGIN
from abrl.db import Session
from abrl.platform_models import Membership, Project, User


def configure_auth(app):
    secret = os.getenv("ABRL_SESSION_SECRET") or OPERATOR_TOKEN or secrets.token_hex(32)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        https_only=WEB_ORIGIN.startswith("https://"),
        same_site="lax",
        max_age=3600,
        session_cookie="abrl_session",
    )
    issuer = os.getenv("ABRL_OIDC_ISSUER", "").rstrip("/")
    client_id = os.getenv("ABRL_OIDC_CLIENT_ID")
    oauth = OAuth()
    if issuer and client_id:
        oauth.register(
            "team",
            server_metadata_url=issuer + "/.well-known/openid-configuration",
            client_id=client_id,
            client_secret=os.getenv("ABRL_OIDC_CLIENT_SECRET"),
            client_kwargs={"scope": "openid email profile"},
        )

    @app.get("/auth/login")
    async def login(request: Request):
        if not oauth.create_client("team"):
            raise HTTPException(503, "OIDC is not configured; use local owner bootstrap")
        return await oauth.team.authorize_redirect(request, str(request.url_for("oidc_callback")))

    @app.get("/auth/callback", name="oidc_callback")
    async def callback(request: Request):
        if not oauth.create_client("team"):
            raise HTTPException(503, "OIDC is not configured")
        token = await oauth.team.authorize_access_token(request)
        info = token.get("userinfo")
        if not info or info.get("iss") != issuer or not info.get("sub"):
            raise HTTPException(401, "Invalid OIDC identity")
        subject = issuer + "|" + info["sub"]
        with Session.begin() as db:
            user = db.scalar(select(User).where(User.subject == subject))
            if not user:
                user = User(subject=subject, email=info.get("email", ""), name=info.get("name", subject))
                db.add(user)
                db.flush()
            # Explicitly configured bootstrap subject only; never promote the first arbitrary login.
            if subject == os.getenv("ABRL_OIDC_OWNER_SUBJECT"):
                if not db.get(Project, "default"):
                    db.add(Project(id="default", name="Default project", concurrency=20))
                    db.flush()
                if not db.get(Membership, ("default", user.id)):
                    db.add(Membership(project_id="default", user_id=user.id, role="owner"))
            request.session.clear()
            request.session["user_id"] = user.id
        return RedirectResponse(WEB_ORIGIN)

    @app.post("/auth/logout")
    def logout(request: Request):
        if request.headers.get("origin") not in (None, WEB_ORIGIN):
            raise HTTPException(403, "Untrusted origin")
        request.session.clear()
        return {"signed_out": True}
