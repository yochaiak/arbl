import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import select

from abrl.config import OPERATOR_TOKEN, WEB_ORIGIN
from abrl.db import Session
from abrl.platform_models import Membership, Project, User

ROLES = {"viewer": 1, "analyst": 2, "operator": 3, "owner": 4}


@dataclass
class Actor:
    user_id: str
    project_id: str
    role: str


def ensure_bootstrap(db) -> User:
    user = db.scalar(select(User).where(User.subject == "local:bootstrap"))
    if user is None:
        user = User(subject="local:bootstrap", name="Local owner")
        db.add(user)
        db.flush()
    if db.get(Project, "default") is None:
        db.add(Project(id="default", name="Default project", concurrency=20))
        db.flush()
    if db.get(Membership, ("default", user.id)) is None:
        db.add(Membership(project_id="default", user_id=user.id, role="owner"))
        db.flush()
    return user


def principal(request: Request) -> str:
    header = request.headers.get("authorization", "")
    prefix, _, token = header.partition(" ")
    if (
        prefix.lower() == "bearer"
        and OPERATOR_TOKEN
        and hmac.compare_digest(token.encode(), OPERATOR_TOKEN.encode())
    ):
        with Session.begin() as db:
            return ensure_bootstrap(db).id
    auth = request.session.get("user_id") if "session" in request.scope else None
    if auth:
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("origin") not in (
            None,
            WEB_ORIGIN,
            str(request.base_url).rstrip("/"),
        ):
            raise HTTPException(403, "Untrusted mutation origin")
        with Session() as db:
            if db.get(User, auth):
                return auth
    raise HTTPException(401, "Sign in to access this project.")


def authorize(request: Request, minimum: str = "viewer") -> Actor:
    user_id = principal(request)
    project_id = request.headers.get("x-project-id", "default")
    with Session() as db:
        membership = db.get(Membership, (project_id, user_id))
        if not membership or ROLES[membership.role] < ROLES[minimum]:
            raise HTTPException(403, "Your project role does not permit this action.")
        return Actor(user_id, project_id, membership.role)


def scoped(db, model, identity: str, actor: Actor):
    value = db.get(model, identity)
    if value is None or value.project_id != actor.project_id:
        raise HTTPException(404, "Record not found in this project.")
    return value
