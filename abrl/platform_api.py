"""Project-scoped session control API. Mutations and their audit events commit together."""

import asyncio
import base64
import json
import os
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import func, select

from abrl.access import authorize, principal, scoped
from abrl.browser_settings import (
    adapter_contract,
    adapter_pool,
    effective_adapter_limit,
    operator_controlled,
    operator_reserved_step,
)
from abrl.challenge_summary import batch_challenge_summary
from abrl.db import Session, now
from abrl.diagnostic_bundle import build_diagnostic_bundle
from abrl.list_queries import name_orders, ordered, text_match
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    Experiment,
    ExperimentRun,
    FleetSlot,
    Membership,
    ProfileSnapshot,
    Project,
    ProxyInventory,
    ProxyLease,
    ProxyPolicy,
    ResearchSnapshot,
    Schedule,
    ScheduledOccurrence,
    ScheduleRevision,
    SessionBatch,
    SessionBatchItem,
    SessionCommand,
    SessionEvent,
    User,
)
from abrl.platform_schemas import (
    BatchInput,
    BatchLaunchInput,
    CommandInput,
    MemberInput,
    ProfileInput,
    ProjectInput,
    ProxyInventoryInput,
    ProxyPolicyInput,
    ScheduleInput,
)
from abrl.platform_service import (
    ACTIVE,
    TERMINAL,
    batch_allocation_adapters,
    batch_ip_summary,
    batch_item_view,
    batch_proxy_check_ip_summary,
    batch_snapshot,
    batch_view,
    create_batch,
    create_batch_item,
    dependency_versions,
    digest,
    effective_persistence,
    event,
    execution_capabilities,
    include_session_context,
    include_session_ips,
    include_session_recordings,
    list_page,
    live_worker_capacity,
    managed_diagnostic_configuration_valid,
    manual_success_check,
    profile_view,
    require_live_batch,
    select_session_proxy,
    session_view,
    session_view_with_workflow,
    settle_batch,
    uses_retired_lab_fixture,
    utc_after,
)
from abrl.platform_views import (
    AdapterView,
    BatchChallengeSummary,
    BatchIPSummary,
    BatchItemView,
    BatchPreflightView,
    BatchProxyCheckIPSummary,
    BatchView,
    InventoryView,
    Page,
    PolicyView,
    ProfileView,
    ProxyPreflightSummary,
    ScheduleView,
    SessionView,
)
from abrl.preflight_summary import preflight_summary
from abrl.scheduling import next_fire, preview, schedule_retirement_reason, scheduled_batch_input
from abrl.schemas import walk_workflow_steps
from abrl.session_metadata import public_session_event_detail

router = APIRouter(prefix="/api/v1")


@router.get("/me")
def me(request: Request):
    return asdict(authorize(request))


@router.get("/projects")
def projects(request: Request):
    user = principal(request)
    with Session() as db:
        return [
            {"id": p.id, "name": p.name, "role": m.role, "concurrency": p.concurrency}
            for p, m in db.execute(
                select(Project, Membership)
                .join(Membership, Membership.project_id == Project.id)
                .where(Membership.user_id == user)
            )
        ]


@router.post("/projects", status_code=201)
def create_project(data: ProjectInput, request: Request):
    actor = authorize(request, "owner")
    with Session.begin() as db:
        row = Project(**data.model_dump())
        db.add(row)
        db.flush()
        db.add(Membership(project_id=row.id, user_id=actor.user_id, role="owner"))
        event(db, row.id, "project_created", actor_id=actor.user_id)
        return {"id": row.id, **data.model_dump(), "role": "owner"}


@router.get("/projects/{project_id}/members")
def members(project_id: str, request: Request):
    actor = authorize(request, "owner")
    if actor.project_id != project_id:
        raise HTTPException(403, "Select this project first")
    with Session() as db:
        return [
            {"user_id": u.id, "subject": u.subject, "name": u.name, "email": u.email, "role": m.role}
            for u, m in db.execute(
                select(User, Membership)
                .join(Membership, Membership.user_id == User.id)
                .where(Membership.project_id == project_id)
            )
        ]


@router.put("/projects/{project_id}/members")
def put_member(project_id: str, data: MemberInput, request: Request):
    actor = authorize(request, "owner")
    if actor.project_id != project_id:
        raise HTTPException(403, "Select this project first")
    with Session.begin() as db:
        db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        user = db.scalar(select(User).where(User.subject == data.subject))
        if not user:
            user = User(subject=data.subject, email=data.email, name=data.email or data.subject)
            db.add(user)
            db.flush()
        member = db.get(Membership, (project_id, user.id))
        if member and member.role == "owner" and data.role != "owner":
            count = db.scalar(
                select(func.count())
                .select_from(Membership)
                .where(Membership.project_id == project_id, Membership.role == "owner")
            )
            if count <= 1:
                raise HTTPException(409, "A project must retain an owner")
        if not member:
            db.add(Membership(project_id=project_id, user_id=user.id, role=data.role))
        else:
            member.role = data.role
        event(db, project_id, "membership_updated", actor_id=actor.user_id, user_id=user.id, role=data.role)
        return {"user_id": user.id, "role": data.role}


@router.get("/adapters", response_model=list[AdapterView])
def adapters(request: Request):
    authorize(request)
    with Session() as db:
        _, capacities, reported_pools = live_worker_capacity(db)
    return [
        {**entry, "worker_capacity": capacities[entry["id"]] if adapter_pool(entry["id"]) in reported_pools else None}
        for entry in execution_capabilities()
    ]


@router.get("/profiles", response_model=Page[ProfileView])
def profiles(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    status: str | None = Query(None, max_length=32),
    adapter: str | None = Query(None, max_length=32),
    persistence: str | None = Query(None, max_length=32),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = select(BrowserProfile).where(BrowserProfile.project_id == actor.project_id)
        if q:
            query = query.where(text_match(BrowserProfile.name, q))
        for column, value in (
            (BrowserProfile.status, status),
            (BrowserProfile.adapter, adapter),
            (BrowserProfile.persistence, persistence),
        ):
            if value:
                query = query.where(column == value)
        return list_page(
            db,
            ordered(query, order, name_orders(BrowserProfile)),
            profile_view,
            limit=limit,
            offset=offset,
        )


@router.post("/profiles", status_code=201, response_model=ProfileView)
def create_profile(data: ProfileInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = BrowserProfile(project_id=actor.project_id, **data.model_dump())
        db.add(row)
        db.flush()
        event(db, actor.project_id, "profile_created", actor_id=actor.user_id, profile_id=row.id)
        return profile_view(row)


@router.get("/profiles/{profile_id}/snapshots")
def profile_snapshots(
    profile_id: str, request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)
):
    actor = authorize(request)
    with Session() as db:
        scoped(db, BrowserProfile, profile_id, actor)
        return list_page(
            db,
            select(ProfileSnapshot)
            .where(ProfileSnapshot.profile_id == profile_id)
            .order_by(ProfileSnapshot.generation.desc()),
            lambda r: {
                k: getattr(r, k) for k in ("id", "generation", "checksum", "environment", "created_at")
            },
            limit=limit,
            offset=offset,
        )


@router.post("/profiles/{profile_id}/{action}")
def profile_action(profile_id: str, action: str, request: Request, data: dict):
    actor = authorize(request, "owner")
    with Session.begin() as db:
        row = db.scalar(select(BrowserProfile).where(BrowserProfile.id == profile_id).with_for_update())
        if not row or row.project_id != actor.project_id:
            raise HTTPException(404, "Profile not found")
        if action == "recover":
            if row.status != "quarantined":
                raise HTTPException(409, "Only quarantined profiles need recovery")
            if (
                data.get("ownership_epoch") != row.epoch
                or data.get("confirmed_stopped") is not True
                or len(str(data.get("evidence", ""))) < 20
            ):
                raise HTTPException(
                    422,
                    "Recovery requires current ownership_epoch, confirmed_stopped=true and runtime termination evidence",
                )
            from abrl.session_runner import release_proxy

            if row.owner_session_id:
                prior = db.get(BrowserSession, row.owner_session_id)
                if prior and prior.status in ACTIVE:
                    raise HTTPException(409, "Stop active runtime before recovery")
                if prior:
                    release_proxy(db, prior)
                    prior.lease_token = None
            row.epoch += 1
            row.owner_session_id = None
            row.status = "available"
            event(
                db,
                actor.project_id,
                "profile_recovered",
                actor_id=actor.user_id,
                profile_id=row.id,
                evidence=data["evidence"],
                epoch=row.epoch,
            )
            return profile_view(row)
        if row.owner_session_id or row.status != "available":
            raise HTTPException(
                409,
                "Profile is owned or quarantined. Confirm the previous runtime has stopped before recovery.",
            )
        if action == "restore":
            snapshot = db.scalar(
                select(ProfileSnapshot).where(
                    ProfileSnapshot.profile_id == row.id,
                    ProfileSnapshot.generation == data.get("generation"),
                )
            )
            if not snapshot:
                raise HTTPException(404, "Snapshot generation not found")
            row.snapshot_key = snapshot.key
        elif action == "reset":
            row.snapshot_key = None
        else:
            raise HTTPException(404, "Unknown profile action")
        event(db, actor.project_id, "profile_" + action, actor_id=actor.user_id, profile_id=row.id)
        return profile_view(row)


@router.get("/sessions", response_model=Page[SessionView])
def sessions(
    request: Request,
    response: Response,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: str | None = Query(None, max_length=32),
    q: str | None = Query(None, max_length=100),
    batch_id: str | None = Query(None, max_length=36),
    adapter: str | None = Query(None, max_length=32),
    mode: str | None = Query(None, max_length=16),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = select(BrowserSession).where(BrowserSession.project_id == actor.project_id)
        if status:
            query = query.where(BrowserSession.status == status)
        if batch_id:
            query = query.where(BrowserSession.batch_id == batch_id)
        if q:
            query = query.where(text_match(BrowserSession.id, q))
        if adapter:
            query = query.where(BrowserSession.adapter == adapter)
        if mode:
            query = query.where(BrowserSession.mode == mode)
        page = list_page(
            db,
            ordered(
                query,
                order,
                {
                    "newest": (BrowserSession.created_at.desc(), BrowserSession.id.desc()),
                    "oldest": (BrowserSession.created_at.asc(), BrowserSession.id.asc()),
                    "status_asc": (BrowserSession.status.asc(), BrowserSession.id.asc()),
                    "duration_desc": (
                        BrowserSession.elapsed_ms.desc().nullslast(),
                        BrowserSession.id.asc(),
                    ),
                },
            ),
            session_view,
            limit=limit,
            offset=offset,
        )
        include_session_context(db, actor.project_id, page["items"])
        if actor.role in {"operator", "owner"}:
            include_session_ips(db, actor.project_id, page["items"])
            include_session_recordings(db, actor.project_id, page["items"])
            response.headers["Cache-Control"] = "no-store"
        return page


@router.get("/sessions/{session_id}", response_model=SessionView)
def session_detail(session_id: str, request: Request, response: Response):
    actor = authorize(request)
    with Session() as db:
        item = session_view_with_workflow(db, scoped(db, BrowserSession, session_id, actor))
        if actor.role in {"operator", "owner"}:
            include_session_ips(db, actor.project_id, [item])
            include_session_recordings(db, actor.project_id, [item])
            response.headers["Cache-Control"] = "no-store"
        return item


@router.get("/sessions/{session_id}/diagnostic-bundle")
def export_session_diagnostic_bundle(
    session_id: str, request: Request, baseline_id: str | None = None, auto_baseline: bool = False,
):
    actor = authorize(request)
    with Session() as db:
        session = scoped(db, BrowserSession, session_id, actor)
        bundle = build_diagnostic_bundle(db, session, baseline_id=baseline_id, auto_baseline=auto_baseline)
    return JSONResponse(bundle, headers={
        "Cache-Control": "no-store",
        "Content-Disposition": 'attachment; filename="abrl-diagnostic-bundle.json"',
    })


def _session_event_detail(row: SessionEvent) -> dict:
    return public_session_event_detail(row.kind, row.detail)


@router.get("/sessions/{session_id}/events")
def session_events(
    session_id: str, request: Request, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)
):
    actor = authorize(request)
    with Session() as db:
        scoped(db, BrowserSession, session_id, actor)
        return list_page(
            db,
            select(SessionEvent)
            .where(SessionEvent.session_id == session_id)
            .order_by(SessionEvent.sequence.desc()),
            lambda r: {"sequence": r.sequence, "kind": r.kind, "created_at": r.created_at,
                       "detail": _session_event_detail(r)},
            limit=limit,
            offset=offset,
        )


@router.get("/sessions/{session_id}/frame")
def session_frame(session_id: str, request: Request):
    actor = authorize(request)
    with Session() as db:
        row = scoped(db, BrowserSession, session_id, actor)
        if row.status != "awaiting_operator":
            raise HTTPException(409, "Viewer is available only during manual control")
        # The current frame is transient, bounded, and never included in list/event responses.
        frame = row.metadata_json.get("viewer_frame")
        if not frame:
            raise HTTPException(404, "Waiting for the browser frame")
        return Response(
            base64.b64decode(frame), media_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )


@router.post("/sessions/{session_id}/commands")
def session_command(session_id: str, data: CommandInput, request: Request):
    import hmac

    from abrl.command_secrets import (
        CommandSecretError,
        command_fingerprint,
        discard_command,
        open_command,
        seal_command,
    )

    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == session_id).with_for_update())
        if not row or row.project_id != actor.project_id:
            raise HTTPException(404, "Session not found")
        if not data.idempotency_key:
            raise HTTPException(422, "A lifecycle command requires idempotency_key")
        prior = db.scalar(
            select(SessionCommand).where(
                SessionCommand.session_id == session_id,
                SessionCommand.idempotency_key == data.idempotency_key,
            )
        )
        payload = data.model_dump(exclude_none=True)
        try:
            request_hash = command_fingerprint(payload, session_id=session_id, actor_id=actor.user_id)
        except CommandSecretError as exc:
            raise HTTPException(503, str(exc)) from None
        if prior:
            if prior.request_hash:
                matches = hmac.compare_digest(prior.request_hash, request_hash)
            else:
                try:
                    matches = (
                        prior.actor_id == actor.user_id
                        and open_command(
                            prior.payload,
                            session_id=session_id,
                            actor_id=prior.actor_id,
                            epoch=prior.epoch,
                            idempotency_key=prior.idempotency_key,
                        )
                        == payload
                    )
                except CommandSecretError:
                    matches = False
            if not matches:
                raise HTTPException(409, "Idempotency key already has different input")
            if not prior.request_hash:
                prior.request_hash = request_hash
                prior.payload = (
                    discard_command(prior.payload)
                    if prior.consumed_at
                    else seal_command(
                        payload,
                        session_id=session_id,
                        actor_id=prior.actor_id,
                        epoch=prior.epoch,
                        idempotency_key=prior.idempotency_key,
                    )[0]
                )
            return session_view_with_workflow(db, row)
        if row.status in TERMINAL:
            raise HTTPException(409, "Session has finished")
        discard_queued = data.command == "stop"
        if data.command == "stop":
            row.status = "cancelled" if row.status == "queued" else "stopping"
            if row.status == "cancelled":
                row.finished_at = now()
                row.admitted = False
                settle_batch(db, row.batch_id)
        else:
            if row.status != "awaiting_operator":
                raise HTTPException(409, "Browser must be waiting for an operator")
            profile = db.get(BrowserProfile, row.profile_id)
            if (
                not row.lease_token
                or not row.lease_until
                or row.lease_until <= now()
                or not profile
                or profile.owner_session_id != row.id
                or profile.epoch != row.epoch
            ):
                raise HTTPException(409, "Browser ownership lease is no longer valid")
            occupied = row.controller_id and row.controller_until and row.controller_until > now()
            if occupied and row.controller_id != actor.user_id:
                raise HTTPException(409, "Another operator holds control")
            discard_queued = data.command == "release_control" or (
                data.command == "claim_control" and not occupied
            )
            if data.command == "claim_control":
                row.controller_id = actor.user_id
                row.metadata_json = {
                    **row.metadata_json,
                    "participants": list(
                        dict.fromkeys([*row.metadata_json.get("participants", []), actor.user_id])
                    ),
                }
                row.controller_until = utc_after(900)
            elif not occupied:
                raise HTTPException(409, "Claim control first")
            elif data.command == "release_control":
                row.controller_id = None
                row.controller_until = None
            else:
                row.controller_until = utc_after(900)
                if data.command == "continue":
                    row.status = "ready"
                    row.controller_id = None
                    row.controller_until = None
        if discard_queued:
            # A released/expired control period cannot leave buffered keystrokes
            # for a later operator, including the same person claiming again.
            pending = db.scalars(
                select(SessionCommand)
                .where(
                    SessionCommand.session_id == row.id,
                    SessionCommand.consumed_at.is_(None),
                )
                .with_for_update()
            ).all()
            for command in pending:
                if not command.request_hash:
                    try:
                        previous = open_command(
                            command.payload,
                            session_id=row.id,
                            actor_id=command.actor_id,
                            epoch=command.epoch,
                            idempotency_key=command.idempotency_key,
                        )
                        command.request_hash = command_fingerprint(
                            previous,
                            session_id=row.id,
                            actor_id=command.actor_id,
                        )
                    except CommandSecretError:
                        pass  # Discard invalid old input; it cannot become a new command.
                command.payload = discard_command(command.payload)
                command.consumed_at = now()
        try:
            encrypted, request_hash = seal_command(
                payload,
                session_id=session_id,
                actor_id=actor.user_id,
                epoch=row.epoch,
                idempotency_key=data.idempotency_key,
            )
        except CommandSecretError as exc:
            raise HTTPException(503, str(exc)) from None
        applied = data.command in {"stop", "claim_control", "release_control"}
        db.add(
            SessionCommand(
                session_id=session_id,
                actor_id=actor.user_id,
                idempotency_key=data.idempotency_key,
                payload=discard_command(encrypted) if applied else encrypted,
                request_hash=request_hash,
                epoch=row.epoch,
                consumed_at=now() if applied else None,
            )
        )
        event(db, actor.project_id, "session_command", session_id, actor.user_id, command=data.command)
        return session_view_with_workflow(db, row)


@router.get("/batches", response_model=Page[BatchView])
def batches(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    status: str | None = Query(None, max_length=32),
    mode: str | None = Query(None, max_length=16),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = select(SessionBatch).where(SessionBatch.project_id == actor.project_id)
        if q:
            query = query.where(text_match(SessionBatch.name, q))
        if status:
            query = query.where(SessionBatch.status == status)
        if mode:
            query = query.where(SessionBatch.config["mode"].as_string() == mode)
        return list_page(
            db,
            ordered(query, order, name_orders(SessionBatch)),
            lambda r: batch_view(db, r),
            limit=limit,
            offset=offset,
        )


@router.post("/batches", status_code=201, response_model=BatchView)
def post_batch(data: BatchLaunchInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        db.scalar(select(Project).where(Project.id == actor.project_id).with_for_update())
        batch = create_batch(
            db,
            data,
            actor.project_id,
            actor_id=actor.user_id,
            require_live=True,
            allow_test_fixture=request.scope.get("abrl.test_fixture") is True,
        )
        return batch_view(db, batch)


@router.post("/batches/preflight", response_model=BatchPreflightView)
def batch_preflight(data: BatchLaunchInput, request: Request):
    actor = authorize(request, "operator")
    require_live_batch(data)
    with Session() as db:
        config = batch_snapshot(db, data, actor.project_id)
        if not request.scope.get("abrl.test_fixture") and uses_retired_lab_fixture(config["target"], config["workflow"]):
            raise HTTPException(409, "The Lab site was retired. Select an authorized site.")
        dependencies = dependency_versions(config)
        notices = batch_readiness(db, data, config, actor.project_id)
        return {
            "dependencies": dependencies,
            "expected_dependencies": digest(dependencies),
            "count": data.count,
            "notices": notices,
        }


def batch_readiness(db, data, config, project_id):
    from abrl.session_runner import candidates

    notices = []
    capabilities = {entry["id"]: entry for entry in execution_capabilities()}
    pools, adapter_capacity, reported_pools = live_worker_capacity(db)
    uncertain = select(BrowserProfile.owner_session_id).where(
        BrowserProfile.status == "quarantined", BrowserProfile.owner_session_id.is_not(None),
    )
    occupied, occupied_by_adapter = Counter(), Counter()
    for pool, adapter, status, count in db.execute(
        select(BrowserSession.pool, BrowserSession.adapter, BrowserSession.status, func.count())
        .where(BrowserSession.status.in_(ACTIVE) | BrowserSession.id.in_(uncertain))
        .group_by(BrowserSession.pool, BrowserSession.adapter, BrowserSession.status)
    ):
        if status in ACTIVE:
            occupied[pool] += count
        occupied_by_adapter[adapter] += count
    project = db.get(Project, project_id)
    if project is not None and data.concurrency > project.concurrency:
        notices.append({"code": "project_capacity", "message": f"This project can run up to {project.concurrency} sessions at once"})
    groups = config.get("group_launch_configs") or []
    if groups:
        entries = [
            (group.adapter, [*group.profile_ids, *([None] * (group.count - len(group.profile_ids)))], settings)
            for group, settings in zip(data.session_groups, groups, strict=True)
        ]
    else:
        profiles_by_adapter = defaultdict(list)
        for index, adapter in enumerate(batch_allocation_adapters(db, data, project_id, data.seed or 0)):
            profiles_by_adapter[adapter].append(data.profile_ids[index] if data.profile_ids else None)
        entries = [(adapter, assigned, config) for adapter, assigned in profiles_by_adapter.items()]
    ids = {profile for _, assigned, _ in entries for profile in assigned if profile}
    profiles = {
        row.id: row for row in db.scalars(select(BrowserProfile).where(
            BrowserProfile.project_id == project_id, BrowserProfile.id.in_(ids)
        ))
    } if ids else {}
    reserved = set(db.scalars(select(BrowserSession.profile_id).where(
        BrowserSession.profile_id.in_(ids), BrowserSession.status.not_in(TERMINAL)
    ))) if ids else set()
    inventory = []
    if any(settings.get("proxy_policy") for _, _, settings in entries):
        inventory = candidates(db, project_id, config["target"], lock_inventory=False)
    for index, (adapter, assigned, settings) in enumerate(entries, start=1):
        capability = capabilities.get(adapter, {})
        group_number = index if groups else None

        def notice(code, message):
            notices.append({"code": code, "message": message, "group": group_number})

        name = capability.get("name") or adapter
        if not capability.get("available") and not adapter_capacity[adapter]:
            notice("adapter_unavailable", capability.get("reason") or "Browser unavailable")
        pool = adapter_pool(adapter)
        if occupied_by_adapter[adapter] >= effective_adapter_limit(adapter, capability.get("max_concurrency")):
            notice("adapter_capacity", f"{name} has reached its session limit")
        if pool not in pools or (pool in reported_pools and not adapter_capacity[adapter]):
            notice("worker_unavailable", f"Waiting for a {pool} worker that supports {name}")
        elif occupied[pool] >= pools[pool] or (
            pool in reported_pools and occupied_by_adapter[adapter] >= adapter_capacity[adapter]
        ):
            notice("worker_capacity", f"Worker slots for {name} are currently busy")
        persistence_modes = {effective_persistence(settings)} if None in assigned else set()
        for profile_id in assigned:
            if not profile_id:
                continue
            profile = profiles.get(profile_id)
            if not profile or profile.adapter != adapter:
                notice("profile_incompatible", "A requested profile is unavailable or uses a different browser")
                continue
            persistence_modes.add(effective_persistence(settings, profile))
            if profile.status != "available" or profile.owner_session_id or profile.id in reserved:
                notice("profile_busy", f"Profile is busy: {profile.name}")
        if operator_controlled(adapter):
            if not manual_success_check(config["workflow"]):
                notice("operator_success_check_required", "Manual Chrome requires a workflow with a success assertion")
            if any(operator_reserved_step(adapter, step["action"]) for step in walk_workflow_steps(config["workflow"]["steps"])):
                notice("operator_reserved_steps", "The operator performs browser interactions before handoff")
        if any(
            config.get("owned_diagnostic") and not managed_diagnostic_configuration_valid(adapter, persistence, config["target"])
            for persistence in persistence_modes or {effective_persistence(settings)}
        ):
            notice("diagnostic_configuration", "Managed diagnostics need a fresh automated session on the owned fixture")
        contract = adapter_contract(adapter)
        if "persistent" in persistence_modes and contract.features["persistent_profiles"] == "requires_entitlement":
            notice("persistent_profile_entitlement", f"{name} checks profile import and export entitlement at launch")
        if policy := settings.get("proxy_policy"):
            choice = select_session_proxy(config, policy, adapter, inventory)
            if choice["status"] == "unavailable":
                notices.append({"code": "proxy_capacity", "message": "Waiting for an eligible proxy binding", "group": group_number})
            elif choice["status"] == "direct":
                notices.append({"code": "proxy_direct_fallback", "message": "This policy permits direct connectivity while no eligible proxy is available", "group": group_number})
    return list({(n["code"], n["message"], n["group"]): n for n in notices}.values())


@router.get("/batches/{batch_id}/items", response_model=Page[BatchItemView])
def batch_items(
    batch_id: str, request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)
):
    actor = authorize(request)
    with Session() as db:
        scoped(db, SessionBatch, batch_id, actor)
        return list_page(
            db,
            select(SessionBatchItem)
            .where(SessionBatchItem.batch_id == batch_id)
            .order_by(SessionBatchItem.ordinal),
            batch_item_view,
            limit=limit,
            offset=offset,
        )


@router.get("/batches/{batch_id}/ip-summary", response_model=BatchIPSummary)
def get_batch_ip_summary(batch_id: str, request: Request):
    actor = authorize(request)
    with Session() as db:
        return batch_ip_summary(db, scoped(db, SessionBatch, batch_id, actor))


@router.get("/batches/{batch_id}/challenge-summary", response_model=BatchChallengeSummary)
def get_batch_challenge_summary(batch_id: str, request: Request, response: Response):
    actor = authorize(request)
    response.headers["Cache-Control"] = "no-store"
    with Session() as db:
        return batch_challenge_summary(db, scoped(db, SessionBatch, batch_id, actor))


@router.get("/batches/{batch_id}/proxy-check-ip-summary", response_model=BatchProxyCheckIPSummary)
def get_batch_proxy_check_ip_summary(batch_id: str, request: Request, response: Response):
    actor = authorize(request)
    response.headers["Cache-Control"] = "no-store"
    with Session() as db:
        return batch_proxy_check_ip_summary(
            db, scoped(db, SessionBatch, batch_id, actor), show_ips=actor.role in {"operator", "owner"}
        )


@router.post("/batches/{batch_id}/retry-creation", response_model=BatchView)
def retry_batch_creation(
    batch_id: str, request: Request, idempotency_key: str = Header(min_length=8, max_length=100)
):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, SessionBatch, batch_id, actor)
        db.refresh(row, with_for_update=True)
        previous = db.scalars(
            select(SessionEvent).where(
                SessionEvent.project_id == actor.project_id,
                SessionEvent.kind == "batch_creation_retried",
            )
        )
        if any(
            e.detail.get("batch_id") == batch_id and e.detail.get("idempotency_key") == idempotency_key
            for e in previous
        ):
            return batch_view(db, row)
        if row.status in ("cancelled", "cancelling") or db.get(ResearchSnapshot, batch_id):
            raise HTTPException(409, "Cancelled or reported batches cannot resume creation")
        if row.config.get("mode") != "live":
            raise HTTPException(409, "Historical simulated batches cannot create new sessions")
        for run in db.scalars(
            select(ExperimentRun)
            .join(Experiment)
            .where(
                Experiment.project_id == actor.project_id,
                ExperimentRun.report_checksum.is_not(None),
            )
        ):
            if batch_id in run.batch_ids:
                raise HTTPException(409, "A frozen experiment report cannot be changed")
        data = BatchInput.model_validate(
            {k: v for k, v in row.config.items() if k in BatchInput.model_fields}
        )
        retried = 0
        for item in db.scalars(
            select(SessionBatchItem)
            .where(
                SessionBatchItem.batch_id == batch_id,
                SessionBatchItem.status == "failed",
            )
            .order_by(SessionBatchItem.ordinal)
            .with_for_update()
        ):
            create_batch_item(db, row, item, data)
            retried += 1
        if row.config.get("experiment_run_id"):
            from abrl.experiment_api import apply_experiment_dispatch

            apply_experiment_dispatch(db, row)
        if row.status == "creation_failed":
            row.status, row.finished_at = "queued", None
        settle_batch(db, batch_id)
        event(
            db,
            actor.project_id,
            "batch_creation_retried",
            actor_id=actor.user_id,
            batch_id=batch_id,
            idempotency_key=idempotency_key,
            count=retried,
        )
        db.flush()
        return batch_view(db, row)


@router.post("/batches/{batch_id}/{action}")
def batch_action(batch_id: str, action: str, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, SessionBatch, batch_id, actor)
        db.refresh(row, with_for_update=True)
        if row.status in ("completed", "cancelled"):
            return batch_view(db, row)
        if action == "pause":
            row.status = "paused"
        elif action == "resume":
            row.status = "queued"
        elif action == "cancel":
            row.status = "cancelling"
            for allocation in db.scalars(
                select(SessionBatchItem)
                .where(
                    SessionBatchItem.batch_id == batch_id,
                    SessionBatchItem.status == "failed",
                )
                .with_for_update()
            ):
                allocation.status = "cancelled"
            for item in db.scalars(
                select(BrowserSession).where(BrowserSession.batch_id == batch_id).with_for_update()
            ):
                if item.status == "queued":
                    item.status = "cancelled"
                    item.admitted = False
                    item.finished_at = now()
                elif item.status in ACTIVE:
                    item.status = "stopping"
            settle_batch(db, batch_id)
        else:
            raise HTTPException(404, "Unknown batch action")
        event(db, actor.project_id, "batch_" + action, actor_id=actor.user_id, batch_id=batch_id)
        return batch_view(db, row)


def policy_view(r):
    return {"id": r.id, "version": r.version, "created_at": r.created_at, **r.config}


def inventory_view(r):
    from abrl.proxy import validate_inventory_config

    try:
        validate_inventory_config(r.config)
        readiness = {"status": "configured", "reason": "Live provider validation remains required"}
    except ValueError as exc:
        readiness = {"status": "unsupported_configuration", "reason": str(exc)}
    return {
        "id": r.id,
        **r.config,
        "enabled": r.enabled,
        "active_leases": r.active_leases,
        "health": r.health,
        "readiness": readiness,
    }


def schedule_view(r):
    retirement = schedule_retirement_reason(r.config)
    return {
        "id": r.id,
        "version": r.version,
        "enabled": r.enabled and retirement is None,
        "next_fire_at": r.next_fire_at if retirement is None else None,
        "pause_reason": retirement,
        "dependencies": dependency_versions(r.config["pinned"]) if r.config.get("pinned") else None,
        "created_at": r.created_at,
        **{k: v for k, v in r.config.items() if k not in ("pinned", "batch")},
        "batch": {"capture_javascript": False, **r.config["batch"]},
    }


@router.get("/proxy-preflight/summary", response_model=ProxyPreflightSummary)
def get_proxy_preflight_summary(request: Request, limit: int = Query(200, ge=1, le=1000)):
    actor = authorize(request)
    with Session() as db:
        return preflight_summary(db, actor.project_id, limit)


@router.get("/proxy-policies", response_model=Page[PolicyView])
def policies(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    continuity: str | None = Query(None, max_length=32),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = select(ProxyPolicy).where(ProxyPolicy.project_id == actor.project_id)
        if q:
            query = query.where(text_match(ProxyPolicy.name, q))
        if continuity:
            query = query.where(ProxyPolicy.config["continuity"].as_string() == continuity)
        return list_page(
            db,
            ordered(query, order, name_orders(ProxyPolicy)),
            policy_view,
            limit=limit,
            offset=offset,
        )


@router.post("/proxy-policies", status_code=201, response_model=PolicyView)
def create_policy(data: ProxyPolicyInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = ProxyPolicy(project_id=actor.project_id, name=data.name, config=data.model_dump())
        db.add(row)
        db.flush()
        event(db, actor.project_id, "proxy_policy_created", actor_id=actor.user_id, policy_id=row.id)
        return policy_view(row)


@router.get("/proxy-inventory", response_model=Page[InventoryView])
def inventory(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    enabled: bool | None = None,
    product: str | None = Query(None, max_length=32),
    country: str | None = Query(None, max_length=2),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = select(ProxyInventory).where(ProxyInventory.project_id == actor.project_id)
        if q:
            query = query.where(text_match(ProxyInventory.name, q))
        if enabled is not None:
            query = query.where(ProxyInventory.enabled == enabled)
        if product:
            query = query.where(ProxyInventory.config["product"].as_string() == product)
        if country:
            query = query.where(ProxyInventory.config["country"].as_string() == country.upper())
        return list_page(
            db,
            ordered(query, order, name_orders(ProxyInventory)),
            inventory_view,
            limit=limit,
            offset=offset,
        )


@router.post("/proxy-inventory", status_code=201, response_model=InventoryView)
def create_inventory(data: ProxyInventoryInput, request: Request):
    actor = authorize(request, "owner")
    with Session.begin() as db:
        row = ProxyInventory(
            project_id=actor.project_id, name=data.name, enabled=data.enabled, config=data.model_dump()
        )
        db.add(row)
        db.flush()
        event(db, actor.project_id, "proxy_inventory_created", actor_id=actor.user_id, inventory_id=row.id)
        return inventory_view(row)


@router.get("/proxy-leases")
def proxy_leases(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    status: str | None = Query(None, max_length=32),
    inventory_id: str | None = Query(None, max_length=36),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = (
            select(ProxyLease)
            .join(BrowserSession, BrowserSession.id == ProxyLease.session_id)
            .where(BrowserSession.project_id == actor.project_id)
        )
        if q:
            query = query.where(text_match(ProxyLease.session_id, q))
        if status:
            query = query.where(ProxyLease.status == status)
        if inventory_id:
            query = query.where(ProxyLease.inventory_id == inventory_id)
        return list_page(
            db,
            ordered(
                query,
                order,
                {
                    "newest": (ProxyLease.created_at.desc(), ProxyLease.id.desc()),
                    "oldest": (ProxyLease.created_at.asc(), ProxyLease.id.asc()),
                },
            ),
            lambda r: {
                **{
                    k: getattr(r, k)
                    for k in (
                        "id",
                        "session_id",
                        "inventory_id",
                        "epoch",
                        "rationale",
                        "status",
                        "created_at",
                        "expires_at",
                        "released_at",
                    )
                },
                "provider": r.config.get("provider", "oxylabs"),
            },
            limit=limit,
            offset=offset,
        )


@router.post("/schedules/preview")
def schedule_preview(data: ScheduleInput, request: Request):
    authorize(request, "operator")
    require_live_batch(data.batch)
    try:
        return {"occurrences": preview(data.model_dump(mode="json"))}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/schedules", response_model=Page[ScheduleView])
def schedules(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, max_length=100),
    enabled: bool | None = None,
    kind: str | None = Query(None, max_length=16),
    order: str = Query("newest", max_length=32),
):
    actor = authorize(request)
    with Session() as db:
        query = select(Schedule).where(Schedule.project_id == actor.project_id)
        if q:
            query = query.where(text_match(Schedule.name, q))
        if enabled is not None:
            query = query.where(Schedule.enabled == enabled)
        if kind:
            query = query.where(Schedule.config["kind"].as_string() == kind)
        return list_page(
            db,
            ordered(
                query,
                order,
                {
                    **name_orders(Schedule),
                    "next_run": (Schedule.next_fire_at.asc().nullslast(), Schedule.id.asc()),
                },
            ),
            schedule_view,
            limit=limit,
            offset=offset,
        )


@router.post("/schedules", status_code=201, response_model=ScheduleView)
def create_schedule(data: ScheduleInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        config = schedule_config(db, data, actor.project_id)
        try:
            following = next_fire(config, datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not following:
            raise HTTPException(422, "Schedule has no future executions")
        row = Schedule(
            project_id=actor.project_id, name=data.name, config=config, next_fire_at=following.isoformat()
        )
        db.add(row)
        db.flush()
        db.add(ScheduleRevision(schedule_id=row.id, version=1, config=config))
        event(db, actor.project_id, "schedule_created", actor_id=actor.user_id, schedule_id=row.id)
        return schedule_view(row)


def schedule_config(db, data, project_id, previous=None):
    require_live_batch(data.batch)
    config = data.model_dump(mode="json")
    config.pop("refresh_dependencies", None)
    config["start_at"] = config["start_at"] or now()
    launch_fields = {"idempotency_key", "expected_dependencies", "expected_workflow_version"}
    saved = BatchInput.model_validate({"capture_javascript": False, **previous["batch"]}) if previous else None
    same_settings = saved and saved.model_dump(exclude=launch_fields) == data.batch.model_dump(exclude=launch_fields)
    if same_settings and not data.refresh_dependencies and not schedule_retirement_reason(previous):
        config["pinned"] = previous["pinned"]
    else:
        config["pinned"] = batch_snapshot(db, data.batch, project_id)
        expected = data.batch.expected_dependencies
        if expected and digest(dependency_versions(config["pinned"])) != expected:
            raise HTTPException(409, "Dependencies changed. Refresh batch readiness before saving.")
    config["batch"]["expected_dependencies"] = None
    if retirement := schedule_retirement_reason(config):
        raise HTTPException(409, retirement)
    return config


@router.get("/schedules/{schedule_id}/occurrences")
def occurrences(
    schedule_id: str, request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)
):
    actor = authorize(request)
    with Session() as db:
        scoped(db, Schedule, schedule_id, actor)
        return list_page(
            db,
            select(ScheduledOccurrence)
            .where(ScheduledOccurrence.schedule_id == schedule_id)
            .order_by(ScheduledOccurrence.scheduled_for.desc()),
            lambda r: {
                k: getattr(r, k) for k in ("id", "version", "scheduled_for", "status", "batch_id", "reason")
            },
            limit=limit,
            offset=offset,
        )


@router.post("/schedules/{schedule_id}/{action}")
def schedule_action(schedule_id: str, action: str, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, Schedule, schedule_id, actor)
        db.refresh(row, with_for_update=True)
        if action in ("resume", "run-now") and (retirement := schedule_retirement_reason(row.config)):
            raise HTTPException(409, retirement)
        if action == "pause":
            row.enabled = False
        elif action == "resume":
            following = next_fire(row.config, datetime.now(UTC))
            if not following:
                raise HTTPException(409, "Schedule has expired")
            row.enabled = True
            row.next_fire_at = following.isoformat()
        elif action == "run-now":
            key = request.headers.get("idempotency-key")
            if not key:
                raise HTTPException(422, "Run now requires Idempotency-Key")
            data = scheduled_batch_input(row.config, key)
            batch = create_batch(
                db,
                data,
                actor.project_id,
                pinned=row.config["pinned"],
                actor_id=actor.user_id,
            )
            prior = db.scalar(
                select(ScheduledOccurrence).where(
                    ScheduledOccurrence.schedule_id == row.id, ScheduledOccurrence.batch_id == batch.id
                )
            )
            if not prior:
                db.add(
                    ScheduledOccurrence(
                        schedule_id=row.id,
                        version=row.version,
                        scheduled_for=now(),
                        status="dispatched",
                        batch_id=batch.id,
                        reason="Explicit operator run-now",
                    )
                )
                event(
                    db,
                    actor.project_id,
                    "schedule_run_now",
                    actor_id=actor.user_id,
                    schedule_id=row.id,
                    batch_id=batch.id,
                )
            return batch_view(db, batch)
        else:
            raise HTTPException(404, "Unknown schedule action")
        event(db, actor.project_id, "schedule_" + action, actor_id=actor.user_id, schedule_id=row.id)
        return schedule_view(row)


def _worker_view(row):
    return {
        "id": row.id,
        "pool": row.pool,
        "capacity": row.capacity,
        "seen_at": row.seen_at,
        "ready": row.seen_at > utc_after(-30),
        "node_name": (row.metadata_json or {}).get("node_name"),
    }


@router.get("/fleet/workers")
def fleet_workers(
    request: Request,
    q: str | None = Query(None, max_length=100),
    pool: str | None = Query(None, pattern="^(standard|vendor)$"),
    ready: bool | None = None,
    order: str = "newest",
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    authorize(request)
    query = select(FleetSlot)
    if q:
        query = query.where(
            text_match(FleetSlot.id, q) | text_match(FleetSlot.metadata_json["node_name"].as_string(), q)
        )
    if pool:
        query = query.where(FleetSlot.pool == pool)
    if ready is not None:
        cutoff = utc_after(-30)
        query = query.where(FleetSlot.seen_at > cutoff if ready else FleetSlot.seen_at <= cutoff)
    query = ordered(
        query,
        order,
        {
            "newest": (FleetSlot.seen_at.desc(), FleetSlot.id.desc()),
            "oldest": (FleetSlot.seen_at.asc(), FleetSlot.id.asc()),
            "name_asc": (FleetSlot.id.asc(),),
            "name_desc": (FleetSlot.id.desc(),),
            "pool_asc": (FleetSlot.pool.asc(), FleetSlot.id.asc()),
        },
    )
    with Session() as db:
        return list_page(db, query, _worker_view, limit=limit, offset=offset)


@router.get("/fleet")
def fleet(request: Request):
    from abrl.costs import fleet_cost

    actor = authorize(request)
    with Session() as db:
        rows = list(db.scalars(select(BrowserSession).where(BrowserSession.project_id == actor.project_id)))
        workers = [_worker_view(r) for r in db.scalars(select(FleetSlot))]
        try:
            pricing = json.loads(os.getenv("ABRL_FLEET_PRICES_JSON", "{}"))
        except (TypeError, ValueError):
            pricing = {}
        return {
            "active": sum(r.status in ACTIVE for r in rows),
            "queued": sum(r.status == "queued" for r in rows),
            "admitted": sum(r.admitted for r in rows),
            "global_limit": 500,
            "workers": workers,
            "pools": [
                {
                    "name": pool,
                    "active": sum(r.pool == pool and r.status in ACTIVE for r in rows),
                    "queued": sum(r.pool == pool and r.status == "queued" for r in rows),
                }
                for pool in ("standard", "vendor")
            ],
            "cost": fleet_cost(workers, pricing),
            "integrations": {},
        }


@router.get("/platform-events")
async def platform_events(request: Request, after: int = 0):
    actor = authorize(request)

    async def stream():
        cursor = after
        for _ in range(60):
            if await request.is_disconnected():
                return
            with Session() as db:
                rows = list(
                    db.scalars(
                        select(SessionEvent)
                        .where(SessionEvent.project_id == actor.project_id, SessionEvent.sequence > cursor)
                        .order_by(SessionEvent.sequence)
                        .limit(500)
                    )
                )
                values = [
                    {"sequence": r.sequence, "kind": r.kind, "session_id": r.session_id,
                     "detail": _session_event_detail(r)}
                    for r in rows
                ]
            for value in values:
                cursor = value["sequence"]
                yield f"id: {cursor}\ndata: {json.dumps(value)}\n\n"
            if not values:
                yield ": heartbeat\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/fleet/metrics", include_in_schema=False)
def fleet_metrics(request: Request):
    import hmac

    expected = os.getenv("ABRL_METRICS_TOKEN", "")
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
        authorize(request, "owner")
    with Session() as db:
        counts = dict(
            db.execute(select(BrowserSession.status, func.count()).group_by(BrowserSession.status))
        )
        oldest = db.scalar(
            select(func.min(BrowserSession.created_at)).where(BrowserSession.status == "queued")
        )
        quarantine = db.scalar(
            select(func.count()).select_from(BrowserProfile).where(BrowserProfile.status == "quarantined")
        )
        lines = [f'abrl_sessions{{status="{status}"}} {count}' for status, count in counts.items()]
        age = max(0, (datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds()) if oldest else 0
        lines.extend(
            [f"abrl_session_queue_oldest_seconds {age}", f"abrl_profiles_quarantined {quarantine}"]
        )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.put("/proxy-inventory/{inventory_id}")
def update_inventory(inventory_id: str, data: ProxyInventoryInput, request: Request):
    actor = authorize(request, "owner")
    with Session.begin() as db:
        row = scoped(db, ProxyInventory, inventory_id, actor)
        db.refresh(row, with_for_update=True)
        if row.active_leases:
            raise HTTPException(409, "Drain active proxy leases before changing inventory")
        row.config = data.model_dump()
        row.name = data.name
        row.enabled = data.enabled
        event(db, actor.project_id, "proxy_inventory_updated", actor_id=actor.user_id, inventory_id=row.id)
        return inventory_view(row)


@router.put("/proxy-policies/{policy_id}")
def update_policy(policy_id: str, data: ProxyPolicyInput, request: Request):
    from abrl.platform_models import ProxyPolicyRevision

    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, ProxyPolicy, policy_id, actor)
        db.refresh(row, with_for_update=True)
        if not db.get(ProxyPolicyRevision, (row.id, row.version)):
            db.add(ProxyPolicyRevision(policy_id=row.id, version=row.version, config=row.config))
        row.version += 1
        row.config = data.model_dump()
        row.name = data.name
        db.add(ProxyPolicyRevision(policy_id=row.id, version=row.version, config=row.config))
        event(
            db,
            actor.project_id,
            "proxy_policy_updated",
            actor_id=actor.user_id,
            policy_id=row.id,
            version=row.version,
        )
        return policy_view(row)


@router.get("/proxy-policies/{policy_id}/versions")
def policy_versions(policy_id: str, request: Request):
    from abrl.platform_models import ProxyPolicyRevision

    actor = authorize(request)
    with Session() as db:
        row = scoped(db, ProxyPolicy, policy_id, actor)
        history = [
            {"version": r.version, **r.config}
            for r in db.scalars(
                select(ProxyPolicyRevision)
                .where(ProxyPolicyRevision.policy_id == policy_id)
                .order_by(ProxyPolicyRevision.version.desc())
            )
        ]
        return history or [{"version": row.version, **row.config}]


@router.put("/schedules/{schedule_id}")
def update_schedule(schedule_id: str, data: ScheduleInput, request: Request):
    actor = authorize(request, "operator")
    with Session.begin() as db:
        row = scoped(db, Schedule, schedule_id, actor)
        db.refresh(row, with_for_update=True)
        config = schedule_config(db, data, actor.project_id, previous=row.config)
        try:
            following = next_fire(config, datetime.now(UTC))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not following:
            raise HTTPException(422, "No future occurrences")
        row.version += 1
        if schedule_retirement_reason(row.config):
            row.enabled = False
        row.config = config
        row.name = data.name
        row.next_fire_at = following.isoformat()
        db.add(ScheduleRevision(schedule_id=row.id, version=row.version, config=config))
        event(
            db,
            actor.project_id,
            "schedule_updated",
            actor_id=actor.user_id,
            schedule_id=row.id,
            version=row.version,
        )
        return schedule_view(row)


@router.put("/projects/{project_id}")
def update_project(project_id: str, data: ProjectInput, request: Request):
    actor = authorize(request, "owner")
    if actor.project_id != project_id:
        raise HTTPException(403, "Select this project first")
    with Session.begin() as db:
        row = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
        row.name = data.name
        row.concurrency = data.concurrency
        event(db, project_id, "project_updated", actor_id=actor.user_id, concurrency=row.concurrency)
        return {"id": row.id, "name": row.name, "concurrency": row.concurrency, "role": actor.role}
