"""Durable session admission and local/Kubernetes one-session execution."""

import argparse
import asyncio
import hashlib
import logging
import os
import platform
import random
import shutil
import signal
import socket
from contextlib import nullcontext, suppress
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, or_, select, text

from abrl.browser import BrowserResult, execute_workflow
from abrl.config import API_ORIGIN, CONTROLLED_ORIGIN, ROOT
from abrl.db import Run, Session, Target, Trial, new_id, now
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    FleetSlot,
    ProfileSnapshot,
    Project,
    ProxyInventory,
    ProxyLease,
    SessionAttempt,
    SessionBatch,
    SessionCommand,
)
from abrl.platform_service import ACTIVE, event, settle_batch, utc_after
from abrl.profile_storage import ProfileStore
from abrl.proxy import lease_proxy_config, product_capabilities, select_proxy
from abrl.telemetry import trace_session

log = logging.getLogger("abrl.sessions")
WORKER = f"{socket.gethostname()}:{os.getpid()}"
STOPPING = False
WORK_ROOT = ROOT / ".abrl" / "sessions"


def serialize_admission(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(8473001)"))


def candidates(db, project_id, session=None):
    inventory = [
        {
            **r.config,
            "id": r.id,
            "status": "healthy" if r.enabled else "paused",
            "entitled": r.enabled,
            "max_connections": r.config["capacity"],
            "active_connections": r.active_leases,
            "remaining_minutes": r.config["max_duration_minutes"],
            **r.health,
            "capabilities": product_capabilities(r.config),
        }
        for r in db.scalars(
            select(ProxyInventory).where(ProxyInventory.project_id == project_id).with_for_update()
        )
    ]
    if (
        session
        and session.attempt_number
        and session.proxy_lease_id
        and session.metadata_json.get("failure_kind") == "transport"
    ):
        previous = db.get(ProxyLease, session.proxy_lease_id)
        if previous and previous.config.get("product") != "residential":
            for candidate in inventory:
                same_binding = all(
                    candidate.get(field) == previous.config.get(field)
                    for field in ("product", "host", "port", "username_ref")
                )
                if candidate["id"] == previous.inventory_id or same_binding:
                    candidate["transport_replacement_excluded"] = True
    return inventory


def admission_tick(db):
    serialize_admission(db)
    uncertain = set(
        db.scalars(
            select(BrowserProfile.owner_session_id).where(
                BrowserProfile.status == "quarantined", BrowserProfile.owner_session_id.is_not(None)
            )
        )
    )
    legacy_active = db.scalar(
        select(func.count())
        .select_from(Trial)
        .join(Run)
        .where(Trial.status == "running", Run.mode == "live")
    )
    legacy_groups = list(
        db.execute(
            select(Run.project_id, Run.target_id, func.count())
            .select_from(Trial)
            .join(Run)
            .where(Trial.status == "running", Run.mode == "live")
            .group_by(Run.project_id, Run.target_id)
        )
    )
    rows = list(
        db.scalars(
            select(BrowserSession)
            .where(or_(BrowserSession.status.in_((*ACTIVE, "queued")), BrowserSession.id.in_(uncertain)))
            .order_by(
                BrowserSession.created_at,
                func.coalesce(BrowserSession.dispatch_order, BrowserSession.ordinal),
                BrowserSession.id,
            )
        )
    )
    previously_admitted = {r.id for r in rows if r.admitted}
    active = [r for r in rows if r.status in ACTIVE or r.id in uncertain]
    for candidate in rows:
        if candidate.status == "queued":
            candidate.admitted = False
    for row in rows:
        if row.status != "queued" or row.admitted:
            continue
        batch = db.get(SessionBatch, row.batch_id)
        if batch.status not in ("queued", "running"):
            continue
        project = db.get(Project, row.project_id)
        if len(active) + legacy_active >= 500:
            break
        if (
            sum(s.project_id == row.project_id for s in active)
            + sum(n for p, t, n in legacy_groups if p == row.project_id)
            >= project.concurrency
        ):
            continue
        if sum(s.batch_id == row.batch_id for s in active) >= batch.concurrency:
            continue
        experiment_run = batch.config.get("experiment_run_id")
        if experiment_run and sum(
            db.get(SessionBatch, session.batch_id).config.get("experiment_run_id") == experiment_run
            for session in active
        ) >= batch.config.get("experiment_concurrency", 1):
            continue
        target = batch.config["target_id"]
        if sum(
            db.get(SessionBatch, s.batch_id).config["target_id"] == target for s in active
        ) >= batch.config["target"].get("concurrency", 1):
            continue
        profile = db.get(BrowserProfile, row.profile_id)
        if profile.status != "available" or profile.owner_session_id:
            continue
        if row.mode == "live":
            from abrl.platform_service import execution_capabilities

            capability = next((a for a in execution_capabilities() if a["id"] == row.adapter), {})
            if not capability.get("available"):
                row.status = "blocked"
                row.error = capability.get("reason", "Adapter unavailable")
                row.finished_at = now()
                event(db, row.project_id, "adapter_unavailable", row.id, reason=row.error)
                settle_batch(db, row.batch_id)
                continue
            maximum = capability.get("max_concurrency") or (
                500
                if row.adapter in ("playwright", "manual_chrome")
                else int(os.getenv("ABRL_VENDOR_CAPACITY", "1"))
            )
            if sum(s.adapter == row.adapter for s in active) >= maximum:
                continue
            policy = batch.config.get("proxy_policy")
            if policy:
                choice = select_proxy(
                    {**policy, "duration_minutes": policy["continuity_minutes"], "adapter": row.adapter},
                    candidates(db, row.project_id, row),
                )
                if choice["status"] == "unavailable":
                    continue
        row.admitted = True
        active.append(row)
        if row.id not in previously_admitted:
            event(db, row.project_id, "session_admitted", row.id, pool=row.pool)


@dataclass
class Work:
    id: str
    lease: str
    profile_id: str
    epoch: int
    config: dict
    mode: str
    adapter: str
    persistence: str
    proxy: dict | None
    snapshot: dict | None


def claim(pool):
    with Session.begin() as db:
        serialize_admission(db)
        rows = db.scalars(
            select(BrowserSession)
            .where(
                BrowserSession.status == "queued",
                BrowserSession.admitted.is_(True),
                BrowserSession.pool == pool,
            )
            .order_by(
                BrowserSession.created_at,
                func.coalesce(BrowserSession.dispatch_order, BrowserSession.ordinal),
                BrowserSession.id,
            )
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            batch = db.get(SessionBatch, row.batch_id)
            if batch.status not in ("queued", "running"):
                row.admitted = False
                continue
            profile = db.scalar(
                select(BrowserProfile).where(BrowserProfile.id == row.profile_id).with_for_update()
            )
            if profile.owner_session_id or profile.status != "available":
                row.admitted = False
                continue
            proxy = None
            if row.mode == "live":
                target = db.scalar(
                    select(Target).where(Target.id == batch.config["target_id"]).with_for_update()
                )
                if (
                    not target
                    or target.verified_version != batch.config["target_version"]
                    or target.version != batch.config["target_version"]
                ):
                    row.status = "blocked"
                    row.error = "Target authorization changed"
                    row.admitted = False
                    settle_batch(db, batch.id)
                    continue
                rate_limit = batch.config["target"]["rate_limit"]
                if batch.config["target"]["mode"] == "instrumented":
                    rate_limit = min(rate_limit, int(os.getenv("ABRL_ADSCORE_RATE_LIMIT", "30")))
                if target.last_started_at and target.last_started_at > utc_after(-60 / rate_limit):
                    continue
                policy = batch.config.get("proxy_policy")
                if policy:
                    choice = select_proxy(
                        {
                            **policy,
                            "duration_minutes": policy["continuity_minutes"],
                            "adapter": row.adapter,
                        },
                        candidates(db, row.project_id, row),
                    )
                    if choice["status"] == "unavailable":
                        row.admitted = False
                        continue
                    if choice["status"] == "selected":
                        inventory = db.get(ProxyInventory, choice["candidate"]["id"])
                        proxy = lease_proxy_config(inventory.config, policy, new_id().replace("-", ""))
                        lease = ProxyLease(
                            session_id=row.id,
                            inventory_id=inventory.id,
                            epoch=profile.epoch + 1,
                            config=proxy,
                            rationale=choice,
                            expires_at=utc_after(
                                min(policy["continuity_minutes"], inventory.config["max_duration_minutes"])
                                * 60
                            ),
                        )
                        db.add(lease)
                        db.flush()
                        row.proxy_lease_id = lease.id
                        inventory.active_leases += proxy["max_connections"]
                target.last_started_at = now()
            profile.epoch += 1
            profile.owner_session_id = row.id
            profile.status = "owned"
            row.epoch = profile.epoch
            row.lease_token = new_id()
            row.lease_until = utc_after(30)
            row.owner_worker = WORKER
            row.status = "starting"
            row.started_at = row.started_at or now()
            row.admitted = False
            row.attempt_number += 1
            batch.status = "running"
            db.add(
                SessionAttempt(
                    id=row.lease_token,
                    session_id=row.id,
                    number=row.attempt_number,
                    epoch=row.epoch,
                    proxy_lease_id=row.proxy_lease_id,
                )
            )
            snapshot = None
            if profile.snapshot_key:
                saved = db.scalar(
                    select(ProfileSnapshot).where(
                        ProfileSnapshot.profile_id == profile.id,
                        ProfileSnapshot.key == profile.snapshot_key,
                    )
                )
                snapshot = {
                    "key": saved.key,
                    "generation": Path(saved.key).stem,
                    "environment": saved.environment,
                    "checksum": saved.checksum,
                }
            event(
                db,
                row.project_id,
                "session_started",
                row.id,
                attempt=row.attempt_number,
                epoch=row.epoch,
                worker=WORKER,
            )
            return Work(
                row.id,
                row.lease_token,
                profile.id,
                row.epoch,
                {**batch.config, "ordinal": row.ordinal},
                row.mode,
                row.adapter,
                row.persistence,
                proxy,
                snapshot,
            )
    return None


def owns_runtime(db, row, work):
    profile = db.get(BrowserProfile, work.profile_id)
    return bool(
        row
        and row.lease_token == work.lease
        and row.epoch == work.epoch
        and row.status in ACTIVE
        and row.lease_until
        and row.lease_until > now()
        and profile
        and profile.owner_session_id == work.id
        and profile.epoch == work.epoch
    )


def heartbeat(work):
    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        if not owns_runtime(db, row, work) or row.status == "stopping":
            return True
        slot = db.get(FleetSlot, WORKER)
        if slot:
            slot.seen_at = now()
        target = db.get(Target, work.config["target_id"])
        if work.mode == "live" and (
            target.version != work.config["target_version"] or target.verified_version != target.version
        ):
            return True
        if row.proxy_lease_id and db.get(ProxyLease, row.proxy_lease_id).expires_at <= now():
            return True
        row.lease_until = utc_after(30)
        return False


def record_activity(work, payload):
    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        if not owns_runtime(db, row, work):
            if payload.get("kind") == "manual_frame":
                return
            raise RuntimeError("Browser ownership lease is no longer valid")
        kind = payload.get("kind", "activity")
        if kind == "manual_frame":
            if row.status not in ("starting", "preparing", "awaiting_operator"):
                return
            image = payload.get("jpeg_base64", "")
            if len(image) <= 1_500_000:
                row.metadata_json = {**row.metadata_json, "viewer_frame": image}
            row.status = "awaiting_operator"
            return
        if kind == "action_started":
            db.get(SessionAttempt, work.lease).started_action = True
        if kind == "preparation_started":
            row.status = "preparing"
        if kind in ("preparation_finished", "workflow_started"):
            row.status = "running"
        event(db, row.project_id, kind, row.id, **{k: v for k, v in payload.items() if k != "kind"})


def poll_control(work):
    from abrl.command_secrets import command_fingerprint, discard_command, open_command

    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        if not owns_runtime(db, row, work) or row.status == "stopping":
            return {"command": "stop"}
        commands = db.scalars(
            select(SessionCommand)
            .where(SessionCommand.session_id == row.id, SessionCommand.consumed_at.is_(None))
            .order_by(SessionCommand.created_at)
            .with_for_update()
        ).all()
        handoff_actor = (
            next(
                (
                    command.actor_id
                    for command in commands
                    if command.epoch == work.epoch and command.payload.get("command") == "continue"
                ),
                None,
            )
            if row.status == "ready"
            else None
        )
        for command in commands:
            body = open_command(
                command.payload,
                session_id=command.session_id,
                actor_id=command.actor_id,
                epoch=command.epoch,
                idempotency_key=command.idempotency_key,
            )
            if not command.request_hash:
                command.request_hash = command_fingerprint(
                    body,
                    session_id=command.session_id,
                    actor_id=command.actor_id,
                )
            command.payload = discard_command(command.payload)
            command.consumed_at = now()
            if command.epoch != work.epoch or body["command"] in (
                "claim_control",
                "release_control",
            ):
                continue
            if body["command"] not in {"continue", "stop"} and not (
                (
                    row.controller_id == command.actor_id
                    and row.controller_until
                    and row.controller_until > now()
                )
                or handoff_actor == command.actor_id
            ):
                continue
            return {k: v for k, v in body.items() if k != "idempotency_key"}
    return None


def snapshot_store():
    key = os.getenv("ABRL_PROFILE_ENCRYPTION_KEY")
    if not key:
        raise ValueError("Configure ABRL_PROFILE_ENCRYPTION_KEY before using persistent profiles")
    return ProfileStore(ROOT / ".abrl" / "profile-snapshots", key)


def profile_s3():
    endpoint = os.getenv("ABRL_PROFILE_STORE_URL")
    if not endpoint:
        return None
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("ABRL_S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("ABRL_S3_SECRET_KEY"),
    )


def release_proxy(db, row):
    if not row.proxy_lease_id:
        return
    lease = db.get(ProxyLease, row.proxy_lease_id)
    if lease.status == "active":
        lease.status = "released"
        lease.released_at = now()
        inventory = db.get(ProxyInventory, lease.inventory_id)
        inventory.active_leases = max(0, inventory.active_leases - lease.config.get("max_connections", 1))


def finish(work, result, saved=None):
    from abrl.costs import record_attempt_usage

    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        profile = db.scalar(
            select(BrowserProfile).where(BrowserProfile.id == work.profile_id).with_for_update()
        )
        if (
            row.lease_token != work.lease
            or profile.epoch != work.epoch
            or profile.owner_session_id != work.id
        ):
            return  # Stale worker cannot save or release a new owner.
        attempt = db.get(SessionAttempt, work.lease)
        attempt.finished_at = now()
        attempt.outcome = result.outcome
        attempt.error = result.error
        attempt.started_action = attempt.started_action or result.started_action
        stopped = result.metadata.get("runtime_stopped", False) or work.mode == "demo"
        proxy_config = None
        if row.proxy_lease_id:
            lease = db.get(ProxyLease, row.proxy_lease_id)
            proxy_config = lease.config
            inventory = db.get(ProxyInventory, lease.inventory_id)
            kind = result.metadata.get("failure_kind")
            if kind in ("authentication", "quota"):
                inventory.enabled = False
                inventory.health = {**inventory.health, "paused_reason": kind}
                event(
                    db, row.project_id, "proxy_pool_paused", row.id, inventory_id=inventory.id, reason=kind
                )
            summary = result.metadata.get("transport_summary")
            if summary:
                good = inventory.health.get("transport_successes", 0) + summary.get("successes", 0)
                bad = inventory.health.get("transport_failures", 0) + summary.get("failures", 0)
                inventory.health = {
                    **inventory.health,
                    "transport_successes": good,
                    "transport_failures": bad,
                    "transport_success_rate": good / (good + bad) if good + bad else None,
                    "latency_ms": summary.get("latency_ms"),
                }
        participants = row.metadata_json.get("participants", [])
        usage_attempts = record_attempt_usage(
            row.metadata_json, result.metadata, attempt_id=attempt.id, proxy_config=proxy_config
        )
        row.metadata_json = {
            **result.metadata,
            "usage_attempts": usage_attempts,
            "input_profile_snapshot": work.snapshot,
            "environment_epoch": row.epoch,
            "participants": participants,
            "participant_id": participants[0] if len(participants) == 1 else None,
        }
        row.elapsed_ms = result.elapsed_ms
        row.error = result.error
        row.outcome = result.outcome
        row.workflow_outcome = result.metadata.get("workflow_outcome", result.outcome)
        row.finished_at = now()
        row.status = "cancelled" if row.status == "stopping" else result.outcome
        if result.metadata.get("needs_reconciliation"):
            row.status = "needs_reconciliation"
        row.lease_until = None
        row.controller_id = None
        row.controller_until = None
        if stopped:
            release_proxy(db, row)
            profile.owner_session_id = None
            profile.status = "available"
            if saved:
                profile.version += 1
                profile.snapshot_key = saved["key"]
                db.add(
                    ProfileSnapshot(
                        profile_id=profile.id,
                        generation=profile.version,
                        epoch=profile.epoch,
                        key=saved["key"],
                        checksum=saved["checksum"],
                        environment=saved["environment"],
                    )
                )
            elif (
                work.persistence == "persistent"
                and work.mode == "live"
                and result.metadata.get("profile_save_failed")
            ):
                profile.status = "quarantined"
        else:
            profile.status = "quarantined"
        # Only a positively classified pre-action transport failure may recover once.
        policy = work.config.get("proxy_policy") or {}
        transport = result.metadata.get("transport_summary") or {}
        proven_pre_action = bool(
            result.metadata.get("failure_kind") == "transport"
            and work.config["target"]["origin"].startswith("https://")
            and not result.metadata.get("non_idempotent_action_started")
            and result.metadata.get("steps_completed") == 0
            and transport.get("attempts", 0) > 0
            and transport.get("successes") == 0
            and transport.get("failures") == transport.get("attempts")
        )
        # Keep the durable action-start marker. The narrower proof is that the
        # broker established no origin tunnel, so initial navigation sent no
        # request to the target; successful connections or uncertain actions
        # can never use this exception.
        if proven_pre_action:
            row.metadata_json = {**row.metadata_json, "pre_action_transport_failure_verified": True}
        retry = (
            result.metadata.get("failure_kind") == "transport"
            and (not attempt.started_action or proven_pre_action)
            and stopped
        )
        if (
            retry
            and row.attempt_number <= policy.get("max_replacements", 1)
            and policy.get("failure_policy", "connection_recovery") == "connection_recovery"
        ):
            row.status = "queued"
            row.finished_at = None
            row.lease_token = None
            event(db, row.project_id, "connection_recovery_queued", row.id, previous_epoch=row.epoch)
        else:
            event(db, row.project_id, "session_finished", row.id, outcome=row.status, epoch=row.epoch)
        settle_batch(db, row.batch_id)


def reap_lost(db):
    for row in db.scalars(
        select(BrowserSession)
        .where(BrowserSession.status.in_(ACTIVE), BrowserSession.lease_until < now())
        .with_for_update(skip_locked=True)
    ):
        attempt = db.get(SessionAttempt, row.lease_token)
        row.status = "needs_reconciliation" if attempt and attempt.started_action else "failed"
        row.error = "Worker lease expired; runtime shutdown must be confirmed before profile recovery"
        row.outcome = "failed"
        row.finished_at = now()
        if attempt:
            attempt.outcome = row.status
            attempt.finished_at = now()
            attempt.error = row.error
        profile = db.get(BrowserProfile, row.profile_id)
        profile.status = "quarantined"
        # Keep ownership and proxy capacity reserved until confirmed stopped.
        event(db, row.project_id, "runtime_quarantined", row.id, worker=row.owner_worker, epoch=row.epoch)
        settle_batch(db, row.batch_id)


@trace_session
async def execute(work):
    directory = WORK_ROOT / work.id / work.lease
    result = None
    saved = None
    browser_started = False

    lost = False

    async def keepalive():
        nonlocal lost
        while True:
            try:
                lost = await asyncio.to_thread(heartbeat, work)
            except Exception:
                lost = True
            if lost:
                return
            await asyncio.sleep(3)

    async def cancelled():
        return lost or STOPPING

    lease_task = asyncio.create_task(keepalive())

    async def emit(payload):
        if payload.get("kind") == "artifact":
            from abrl.session_artifacts import store_artifact

            try:
                await asyncio.to_thread(store_artifact, work, payload)
            except Exception:
                await asyncio.to_thread(
                    record_activity,
                    work,
                    {"kind": "artifact_capture_failed", "reason": "Encrypted artifact storage unavailable"},
                )
            return
        await asyncio.to_thread(record_activity, work, payload)

    async def control():
        return await asyncio.to_thread(poll_control, work)

    try:
        if work.mode == "demo":
            await asyncio.sleep(0.04)
            rng = random.Random(f"{work.config['seed']}:{work.config['ordinal']}:{work.adapter}")
            outcome = "completed" if rng.random() > 0.12 else "failed"
            result = BrowserResult(
                outcome,
                rng.randint(200, 1200),
                None if outcome == "completed" else "Simulated workflow failure",
                {
                    "simulated": True,
                    "evidence_source": "simulation",
                    "runtime_stopped": True,
                    "environment": "deterministic-demo",
                },
                False,
            )
        else:
            cloud = os.getenv("ABRL_DEPLOYMENT") == "cloud"
            if work.persistence == "persistent":
                store = snapshot_store()
                if work.snapshot:
                    environment = work.snapshot["environment"]
                    if (
                        environment.get("os") != platform.system()
                        or environment.get("architecture") != platform.machine()
                    ):
                        raise ValueError(
                            "Persistent snapshot cannot cross operating-system or architecture boundaries"
                        )
                    s3 = profile_s3()
                    path = store.root / work.profile_id / f"{work.snapshot['generation']}.abrl"
                    if not path.exists() and s3:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(
                            s3.download_file,
                            os.getenv("ABRL_PROFILE_BUCKET", "abrl-profiles"),
                            work.snapshot["key"],
                            str(path),
                        )
                    if hashlib.sha256(path.read_bytes()).hexdigest() != work.snapshot["checksum"]:
                        raise ValueError(
                            "Snapshot ciphertext checksum does not match the immutable database manifest"
                        )
                    await asyncio.to_thread(
                        store.restore,
                        work.profile_id,
                        work.snapshot["generation"],
                        directory,
                        compatibility=environment,
                    )
                else:
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            preparation = {**work.config["preparation"], "seed": work.config["seed"]}
            steps = work.config["workflow"]["steps"]
            if work.config["target"]["mode"] == "instrumented":
                from urllib.parse import urlencode

                from abrl.observations import context_token

                token = context_token(work.id, work.lease, work.epoch)
                if work.config["target"]["origin"] in (API_ORIGIN, CONTROLLED_ORIGIN):
                    steps = [
                        {
                            **steps[0],
                            "url": "/lab/research?"
                            + urlencode({"context": token, "next": steps[0]["url"]}),
                        },
                        *steps[1:],
                    ]
                else:
                    separator = "&" if "?" in steps[0]["url"] else "?"
                    steps = [
                        {
                            **steps[0],
                            "url": steps[0]["url"] + separator + urlencode({"abrl_context": token}),
                        },
                        *steps[1:],
                    ]
            remote_context = nullcontext(None)
            if cloud:
                from abrl.cloud_runtime import CloudRuntime

                remote_context = CloudRuntime(work.id, work.epoch).launch(
                    {
                        "adapter": work.adapter,
                        "persistence": work.persistence,
                        "origin": work.config["target"]["origin"],
                        "navigation_origins": work.config["target"].get("navigation_origins", []),
                        "resource_origins": work.config["target"]["resource_origins"],
                        "proxy": work.proxy,
                        "traffic_budget_bytes": work.config.get("traffic_budget_bytes", 104857600),
                        "timeout_seconds": work.config["timeout_seconds"]
                        + (
                            preparation.get("lifetime_seconds", 3600)
                            if preparation["mode"] == "manual"
                            else preparation.get("max_seconds", 90)
                            if preparation["mode"] == "automated"
                            else 0
                        ),
                    }
                )
            browser_started = True
            async with remote_context as remote:
                result = await execute_workflow(
                    origin=work.config["target"]["origin"],
                    navigation_origins=work.config["target"].get("navigation_origins", []),
                    resource_origins=work.config["target"]["resource_origins"],
                    steps=steps,
                    cancel=cancelled,
                    timeout_seconds=work.config["timeout_seconds"],
                    adapter=work.adapter,
                    profile_dir=str(directory) if work.persistence == "persistent" else None,
                    expected_compatibility=work.snapshot["environment"] if work.snapshot else None,
                    preparation=preparation,
                    proxy=work.proxy,
                    emit=emit,
                    control=control,
                    remote=remote.connection if remote else None,
                    artifact_policy=work.config.get("artifact_policy", "none"),
                    request_budget=work.config.get("request_budget", 1000),
                    traffic_budget_bytes=work.config.get("traffic_budget_bytes", 104857600),
                )
            if remote:
                result.metadata.update(remote.metadata)
                result.metadata["runtime_stopped"] = remote.stopped and result.metadata.get(
                    "runtime_stopped", False
                )
            if (
                work.persistence == "persistent"
                and result.metadata.get("runtime_stopped")
                and result.metadata.get("profile_saved")
                and not result.metadata.get("cleanup_error")
            ):
                try:
                    with Session() as db:
                        generation = f"{db.get(BrowserProfile, work.profile_id).version + 1}-{work.epoch}"
                    environment = {
                        k: result.metadata.get(k)
                        for k in (
                            "adapter",
                            "browser_version",
                            "os",
                            "architecture",
                            "engine_version",
                            "wrapper_version",
                        )
                        if result.metadata.get(k) is not None
                    }
                    manifest = await asyncio.to_thread(
                        store.save,
                        work.profile_id,
                        directory,
                        clean_shutdown=True,
                        compatibility=environment,
                        generation=generation,
                    )
                    key = f"profiles/{work.profile_id}/{generation}.abrl"
                    s3 = profile_s3()
                    if s3:
                        await asyncio.to_thread(
                            s3.upload_file,
                            manifest["path"],
                            os.getenv("ABRL_PROFILE_BUCKET", "abrl-profiles"),
                            key,
                        )
                    saved = {
                        "key": key,
                        "checksum": manifest["encrypted_sha256"],
                        "environment": environment,
                    }
                except Exception:
                    result.metadata["profile_save_failed"] = True
                    result.error = "Profile snapshot failed; profile quarantined"
                    result.metadata["workflow_outcome"] = result.metadata.get(
                        "workflow_outcome", result.outcome
                    )
                    result.outcome = "failed"
    except asyncio.CancelledError:
        result = BrowserResult("cancelled", 0, "Runner stopping", {"runtime_stopped": False}, False)
        raise
    except Exception as exc:
        log.warning("Session launch/persistence failed: %s", type(exc).__name__)
        if result:
            result.outcome = "failed"
            result.error = "Cloud runtime termination could not be confirmed"
            result.metadata["runtime_stopped"] = False
            result.metadata["needs_reconciliation"] = result.metadata.get(
                "non_idempotent_action_started", False
            )
        else:
            result = BrowserResult(
                "failed",
                0,
                str(exc) if isinstance(exc, ValueError) else "Session infrastructure unavailable",
                {"runtime_stopped": not browser_started},
                False,
            )
    finally:
        lease_task.cancel()
        with suppress(asyncio.CancelledError):
            await lease_task
        if result:
            await asyncio.to_thread(finish, work, result, saved)
        if result and result.metadata.get("runtime_stopped") and directory.exists():
            shutil.rmtree(directory)


def fleet_touch(pool, capacity):
    with Session.begin() as db:
        entry = db.get(FleetSlot, WORKER)
        if entry:
            entry.seen_at = now()
            entry.capacity = capacity
            entry.metadata_json = {**entry.metadata_json, "node_name": os.getenv("ABRL_NODE_NAME")}
        else:
            db.add(
                FleetSlot(
                    id=WORKER,
                    pool=pool,
                    capacity=capacity,
                    seen_at=now(),
                    metadata_json={"node_name": os.getenv("ABRL_NODE_NAME")},
                )
            )


async def run(once=False, pool="standard", capacity=1):
    global STOPPING
    STOPPING = False
    loop = asyncio.get_running_loop()

    def stop():
        global STOPPING
        STOPPING = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop)
    tasks = set()
    try:
        while not STOPPING:
            await asyncio.to_thread(fleet_touch, pool, capacity)
            tasks = {task for task in tasks if not task.done()}
            while len(tasks) < capacity:
                work = await asyncio.to_thread(claim, pool)
                if not work:
                    break
                tasks.add(asyncio.create_task(execute(work)))
                if once:
                    await asyncio.gather(*tasks)
                    return
            if once:
                return
            await asyncio.sleep(1)
    finally:
        # SIGTERM stops admission first and lets cancellation close/save each browser.
        try:
            async with asyncio.timeout(110):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--pool", choices=("standard", "vendor"), default="standard")
    args = parser.parse_args()
    asyncio.run(
        run(
            args.once,
            args.pool,
            1 if args.once else max(1, min(500, int(os.getenv("ABRL_LOCAL_CONCURRENCY", "2")))),
        )
    )
