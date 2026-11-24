"""Durable session admission and local browser execution."""

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
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as DatabaseTimeout

from abrl.batch_presets import preset_details
from abrl.browser import (
    BrowserResult,
    StepFailed,
    effective_preparation,
    execute_workflow,
)
from abrl.browser_diagnostics import (
    confirmed_sticky_timezone,
    diagnostic_report,
    diagnostic_steps,
    is_managed_diagnostic,
)
from abrl.browser_network_trace import public_transport_checks, public_transport_summary
from abrl.browser_settings import adapter_pool, effective_adapter_limit, operator_controlled
from abrl.config import ROOT
from abrl.db import Run, Session, Target, Trial, new_id, now
from abrl.diagnostic_bundle_lifecycle import public_lifecycle_failure
from abrl.javascript_capture import JavaScriptCapture
from abrl.lifecycle import LifecycleTimeline, public_duration_ms, public_lifecycle_timeline
from abrl.network import (
    PROXY_DESTINATION_DENIED,
    PROXY_TRANSPORT_REASONS,
    check_proxy_ip,
    first_proxy_tunnel_failure,
)
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    FleetSlot,
    ProfileSnapshot,
    Project,
    ProviderObservation,
    ProxyInventory,
    ProxyLease,
    SessionAttempt,
    SessionBatch,
    SessionBatchItem,
    SessionCommand,
)
from abrl.platform_service import (
    ACTIVE,
    TERMINAL,
    event,
    live_worker_capacity,
    managed_diagnostic_configuration_valid,
    manual_success_check,
    select_session_proxy,
    session_launch_config,
    settle_batch,
    utc_after,
)
from abrl.profile_storage import ProfileStorageError, ProfileStore
from abrl.proxy import (
    ProxyBinding,
    ProxyConfigurationError,
    lease_proxy_config,
    product_capabilities,
    proxy_scope_allows_target,
)
from abrl.proxy_accounts import AccountCapacity
from abrl.proxy_preflight import run_decodo_preflight
from abrl.session_artifacts import store_javascript_artifact, store_recording_segment
from abrl.session_metadata import public_viewport_conformance, redact_session_metadata, redact_session_text
from abrl.target_authorization import target_is_live_ready
from abrl.telemetry import trace_session

log = logging.getLogger("abrl.sessions")
WORKER = f"{socket.gethostname()}:{os.getpid()}"
STOPPING = False
WORK_ROOT = ROOT / ".abrl" / "sessions"
RETRY_DELAYS_SECONDS = (2, 5)


def serialize_admission(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(8473001)"))
    elif db.bind.dialect.name == "sqlite" and os.getenv("ABRL_PROXY_ACCOUNT_GROUPS"):
        # Take SQLite's write lock before reading shared account occupancy.
        db.execute(text("UPDATE proxy_leases SET status = status WHERE 0"))


def _inventory_entry(row):
    return {
        **row.config,
        "id": row.id,
        "status": "healthy" if row.enabled else "paused",
        "entitled": row.enabled,
        "max_connections": row.config["capacity"],
        "active_connections": row.active_leases,
        "remaining_minutes": row.config["max_duration_minutes"],
        **row.health,
        "capabilities": product_capabilities(row.config),
    }


def candidates(
    db, project_id, target, session=None, *, inventory_rows: list[dict] | None = None,
    lock_inventory=True, account_capacity: AccountCapacity | None = None,
):
    if inventory_rows is None:
        query = select(ProxyInventory).where(ProxyInventory.project_id == project_id)
        if lock_inventory:
            query = query.with_for_update()
        inventory_rows = [_inventory_entry(row) for row in db.scalars(query)]
    inventory = [
        row.copy() for row in inventory_rows if proxy_scope_allows_target(row.get("target_origin"), target)
    ]
    if account_capacity is None:
        account_capacity = AccountCapacity.load(db)
    if account_capacity.groups:
        for candidate in inventory:
            group = account_capacity.group(candidate)
            if group is not None and account_capacity.remaining[group] <= 0:
                candidate["account_capacity_exhausted"] = True
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


def _site_rate_reached(target, target_config):
    if not target or not target.last_started_at:
        return False
    rate_limit = target_config.get("rate_limit", 30)
    if target_config.get("mode") == "instrumented":
        try:
            configured_limit = int(os.getenv("ABRL_ADSCORE_RATE_LIMIT", "30"))
        except ValueError:
            configured_limit = 30
        rate_limit = min(rate_limit, max(1, configured_limit))
    return target.last_started_at > utc_after(-60 / rate_limit)


def admission_tick(db, *, require_worker_heartbeat=False):
    from abrl.platform_service import execution_capabilities

    serialize_admission(db)
    uncertain = set(
        db.scalars(
            select(BrowserProfile.owner_session_id).where(
                BrowserProfile.status == "quarantined", BrowserProfile.owner_session_id.is_not(None)
            )
        )
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
        db.execute(
            select(BrowserSession, SessionBatch, BrowserProfile, Project)
            .join(SessionBatch, BrowserSession.batch_id == SessionBatch.id)
            .join(BrowserProfile, BrowserSession.profile_id == BrowserProfile.id)
            .join(Project, BrowserSession.project_id == Project.id)
            .where(or_(BrowserSession.status.in_((*ACTIVE, "queued")), BrowserSession.id.in_(uncertain)))
            .order_by(
                BrowserSession.created_at,
                func.coalesce(BrowserSession.dispatch_order, BrowserSession.ordinal),
                BrowserSession.id,
            )
        )
    )
    queued = [row for row, *_ in rows if row.status == "queued"]
    if not queued:
        return
    capabilities = {
        item["id"]: item for item in execution_capabilities()
    } if any(row.mode == "live" for row in queued) else {}
    launch_config_batches = {
        batch.id for row, batch, *_ in rows
        if row.status == "queued" and (batch.config.get("session_groups") or batch.config.get("batch_presets"))
    }
    items = {
        (item.batch_id, item.ordinal): item for item in db.scalars(
            select(SessionBatchItem).where(SessionBatchItem.batch_id.in_(launch_config_batches))
        )
    } if launch_config_batches else {}
    worker_capacity, adapter_capacity, reported_pools = (
        live_worker_capacity(db) if require_worker_heartbeat else ({}, {}, set())
    )
    current_time = now()
    total_active = sum(count for _, _, count in legacy_groups)
    by_project = Counter()
    by_batch = Counter()
    by_target = Counter()
    demo_exclusions = Counter()
    by_experiment = Counter()
    by_adapter = Counter()
    by_pool = Counter()
    for project_id, _, count in legacy_groups:
        by_project[project_id] += count

    for row, batch, *_ in rows:
        if row.status not in ACTIVE and row.id not in uncertain:
            continue
        total_active += 1
        by_project[row.project_id] += 1
        by_batch[row.batch_id] += 1
        by_target[batch.config["target_id"]] += 1
        by_experiment[batch.config.get("experiment_run_id")] += 1
        by_adapter[row.adapter] += 1
        if row.status in ACTIVE:
            by_pool[row.pool] += 1
        if row.mode == "live" and row.id in uncertain and row.status in TERMINAL:
            demo_exclusions[batch.config["target_id"]] += 1

    inventory_by_project = {}
    account_capacity = None
    targets = {}
    for row, batch, profile, project in rows:
        if row.status != "queued":
            continue
        was_admitted = row.admitted
        row.admitted = False
        if batch.status not in ("queued", "running"):
            row.queue_reason = "batch_paused"
            continue
        if row.next_attempt_at and row.next_attempt_at > current_time:
            row.queue_reason = "retry_backoff"
            continue
        if total_active >= 500:
            row.queue_reason = "global_capacity"
            continue
        if by_project[row.project_id] >= project.concurrency:
            row.queue_reason = "project_capacity"
            continue
        if by_batch[row.batch_id] >= batch.concurrency:
            row.queue_reason = "batch_capacity"
            continue
        experiment_run = batch.config.get("experiment_run_id")
        if experiment_run and by_experiment[experiment_run] >= batch.config.get("experiment_concurrency", 1):
            row.queue_reason = "experiment_capacity"
            continue
        target_id = batch.config["target_id"]
        target_active = by_target[target_id] - (demo_exclusions[target_id] if row.mode == "demo" else 0)
        if target_active >= batch.config["target"].get("concurrency", 1):
            row.queue_reason = "site_capacity"
            continue
        if profile.status != "available" or profile.owner_session_id:
            row.queue_reason = "profile_busy"
            continue
        if row.mode == "live":
            capability = capabilities.get(row.adapter, {})
            available_workers = adapter_capacity.get(row.adapter, 0)
            if not capability.get("available") and not available_workers:
                row.status = "blocked"
                row.error = capability.get("reason", "Adapter unavailable")
                row.finished_at = now()
                row.queue_reason = None
                event(db, row.project_id, "adapter_unavailable", row.id, reason=row.error)
                settle_batch(db, row.batch_id)
                continue
            if operator_controlled(row.adapter) and not manual_success_check(batch.config["workflow"]):
                row.status = row.outcome = "blocked"
                row.error = "Manual Chrome requires a workflow with a success assertion"
                row.finished_at = now()
                row.queue_reason = None
                event(db, row.project_id, "session_finished", row.id, outcome=row.status,
                      reason="operator_success_check_required")
                settle_batch(db, row.batch_id)
                continue
            maximum = effective_adapter_limit(row.adapter, capability.get("max_concurrency"))
            if by_adapter[row.adapter] >= maximum:
                row.queue_reason = "adapter_capacity"
                continue
            if require_worker_heartbeat and (
                row.pool not in worker_capacity or (row.pool in reported_pools and not available_workers)
            ):
                row.queue_reason = "worker_unavailable"
                continue
            if require_worker_heartbeat and by_pool[row.pool] >= worker_capacity[row.pool]:
                row.queue_reason = "worker_capacity"
                continue
            if row.pool in reported_pools and by_adapter[row.adapter] >= available_workers:
                row.queue_reason = "worker_capacity"
                continue
            config = session_launch_config(
                db, row, batch, item=items.get((row.batch_id, row.ordinal)), resolve_workflow=False
            )
            if require_worker_heartbeat:
                if target_id not in targets:
                    targets[target_id] = db.get(Target, target_id)
                if _site_rate_reached(targets[target_id], config["target"]):
                    row.queue_reason = "site_rate_limit"
                    continue
            policy = config.get("proxy_policy")
            if policy:
                if account_capacity is None:
                    account_capacity = AccountCapacity.load(db)
                if row.project_id not in inventory_by_project:
                    inventory_by_project[row.project_id] = [
                        _inventory_entry(inventory)
                        for inventory in db.scalars(
                            select(ProxyInventory).where(ProxyInventory.project_id == row.project_id)
                        )
                    ]
                choice = select_session_proxy(
                    config, policy, row.adapter,
                    candidates(
                        db, row.project_id, config["target"], row,
                        inventory_rows=inventory_by_project[row.project_id], account_capacity=account_capacity,
                    ),
                )
                if choice["status"] == "unavailable":
                    row.queue_reason = "proxy_capacity"
                    continue
                if choice["status"] == "selected" and account_capacity.groups:
                    account_capacity.reserve(choice["candidate"])
        row.admitted = True
        row.queue_reason = None
        total_active += 1
        by_project[row.project_id] += 1
        by_batch[row.batch_id] += 1
        by_target[target_id] += 1
        by_experiment[experiment_run] += 1
        by_adapter[row.adapter] += 1
        by_pool[row.pool] += 1
        if not was_admitted:
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
    attempt_number: int


def claim(pool, *, supported_adapters=None):
    adapter_scope = () if supported_adapters is None else (
        or_(BrowserSession.mode != "live", BrowserSession.adapter.in_(supported_adapters)),
    )
    with Session.begin() as db:
        serialize_admission(db)
        rows = db.scalars(
            select(BrowserSession)
            .where(
                BrowserSession.status == "queued",
                BrowserSession.admitted.is_(True),
                BrowserSession.pool == pool,
                *adapter_scope,
                or_(BrowserSession.next_attempt_at.is_(None), BrowserSession.next_attempt_at <= now()),
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
                row.queue_reason = "batch_paused"
                continue
            config = session_launch_config(db, row, batch)
            profile = db.scalar(
                select(BrowserProfile).where(BrowserProfile.id == row.profile_id).with_for_update()
            )
            if profile.owner_session_id or profile.status != "available":
                row.admitted = False
                row.queue_reason = "profile_busy"
                continue
            snapshot = None
            if row.persistence == "persistent" and profile.snapshot_key:
                saved = db.scalar(
                    select(ProfileSnapshot).where(
                        ProfileSnapshot.profile_id == profile.id,
                        ProfileSnapshot.key == profile.snapshot_key,
                    )
                )
                if saved is None:
                    row.status = row.outcome = "failed"
                    row.error = "Profile snapshot manifest is missing"
                    row.finished_at = now()
                    row.admitted = False
                    row.queue_reason = None
                    event(
                        db, row.project_id, "session_finished", row.id, outcome=row.status, reason=row.error
                    )
                    settle_batch(db, batch.id)
                    continue
                snapshot = {
                    "key": saved.key,
                    "generation": Path(saved.key).stem,
                    "environment": saved.environment,
                    "checksum": saved.checksum,
                }
            proxy = None
            proxy_lease_id = None
            if row.mode == "live":
                target = db.scalar(
                    select(Target).where(Target.id == config["target_id"]).with_for_update()
                )
                if not target_is_live_ready(
                    target, config["target_version"], config["target"]
                ):
                    row.status = row.outcome = "blocked"
                    row.error = "Target authorization changed"
                    row.admitted = False
                    row.queue_reason = None
                    row.finished_at = now()
                    event(db, row.project_id, "session_finished", row.id, outcome=row.status, reason=row.error)
                    settle_batch(db, batch.id)
                    continue
                if _site_rate_reached(target, config["target"]):
                    row.admitted = False
                    row.queue_reason = "site_rate_limit"
                    continue
                policy = config.get("proxy_policy")
                if policy:
                    choice = select_session_proxy(
                        config, policy, row.adapter,
                        candidates(db, row.project_id, config["target"], row),
                    )
                    if choice["status"] == "unavailable":
                        row.admitted = False
                        row.queue_reason = "proxy_capacity"
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
                        proxy_lease_id = lease.id
                        inventory.active_leases += proxy["max_connections"]
                target.last_started_at = now()
            profile.epoch += 1
            profile.owner_session_id = row.id
            profile.status = "owned"
            row.epoch = profile.epoch
            row.lease_token = new_id()
            row.proxy_lease_id = proxy_lease_id
            row.lease_until = utc_after(30)
            row.owner_worker = WORKER
            row.status = "starting"
            row.started_at = row.started_at or now()
            row.admitted = False
            row.queue_reason = None
            row.next_attempt_at = None
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
                {**config, "ordinal": row.ordinal},
                row.mode,
                row.adapter,
                row.persistence,
                proxy,
                snapshot,
                row.attempt_number,
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


def _proxy_lease_issue(db, row, work):
    if work.proxy is None:
        return None
    # A heartbeat can precede a long actionability wait. Recheck the original
    # attempt's lease at dispatch without loading its credential configuration.
    lease = db.execute(
        select(ProxyLease.status, ProxyLease.expires_at)
        .join(SessionAttempt, SessionAttempt.proxy_lease_id == ProxyLease.id)
        .where(
            SessionAttempt.id == work.lease, ProxyLease.id == row.proxy_lease_id,
            ProxyLease.session_id == work.id, ProxyLease.epoch == work.epoch,
        )
    ).one_or_none()
    if lease is None or lease.status != "active":
        return "proxy_lease_unavailable"
    return "proxy_lease_expired" if lease.expires_at <= now() else None


def _diagnostic_proxy_ready(work, policy):
    if not work.proxy or work.proxy.get("provider", "oxylabs") != policy.get("provider", "oxylabs"):
        return False
    with Session() as db:
        row = db.get(BrowserSession, work.id)
        return owns_runtime(db, row, work) and row.status != "stopping" and not _proxy_lease_issue(db, row, work)


def heartbeat(work):
    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        if not owns_runtime(db, row, work) or row.status == "stopping":
            return True
        slot = db.get(FleetSlot, WORKER)
        if slot:
            slot.seen_at = now()
        if work.mode == "live":
            target = db.get(Target, work.config["target_id"])
            if not target_is_live_ready(target, work.config["target_version"], work.config["target"]):
                return True
        if issue := _proxy_lease_issue(db, row, work):
            event(db, row.project_id, issue, row.id, attempt=row.attempt_number)
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
        if row.status == "stopping":
            if kind == "action_started":
                raise RuntimeError("Session is stopping")
            if kind in {"manual_frame", "recording_started", "preparation_started", "preparation_finished", "workflow_started"}:
                return
        if kind == "recording_started":
            db.get(SessionAttempt, work.lease).recording_status = "recording"
            return
        if kind == "manual_frame":
            if row.status not in ("starting", "preparing", "awaiting_operator"):
                return
            image = payload.get("jpeg_base64", "")
            if len(image) <= 1_500_000:
                row.metadata_json = {**row.metadata_json, "viewer_frame": image}
            row.status = "awaiting_operator"
            return
        if kind == "action_started":
            if issue := _proxy_lease_issue(db, row, work):
                raise StepFailed(f"Cannot start action ({issue})", reason=issue)
            attempt = db.get(SessionAttempt, work.lease)
            attempt.started_action = True
            if payload.get("non_idempotent") is True:
                row.metadata_json = {**row.metadata_json, "non_idempotent_action_attempt": attempt.number}
        if kind == "proxy_preflight":
            row.metadata_json = {
                **row.metadata_json,
                "proxy_preflight": redact_session_metadata({k: v for k, v in payload.items() if k != "kind"}),
            }
        if kind == "launched":
            device_metadata = redact_session_metadata({
                key: payload[key]
                for key in ("browser_engine", "browser_version", "device_preset", "device_mode", "viewport", "profile_launch_state")
                if key in payload
            })
            if (conformance := public_viewport_conformance(payload.get("viewport_conformance"))) is not None:
                device_metadata.update(viewport_conformance=conformance, viewport_conformance_epoch=row.epoch)
            row.metadata_json = {
                **row.metadata_json,
                **device_metadata,
            }
        if kind == "preparation_started":
            row.status = "preparing"
        if kind in ("preparation_finished", "workflow_started"):
            row.status = "running"
        event(db, row.project_id, kind, row.id, **{k: v for k, v in payload.items() if k != "kind"})


def poll_control(work):
    from abrl.command_secrets import command_fingerprint, discard_command, open_command

    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        if not owns_runtime(db, row, work) or row.status == "stopping" or _proxy_lease_issue(db, row, work):
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
        raise ProfileStorageError("Configure ABRL_PROFILE_ENCRYPTION_KEY before using persistent profiles")
    return ProfileStore(ROOT / ".abrl" / "profile-snapshots", key)


def profile_s3(*, config=None):
    endpoint = os.getenv("ABRL_PROFILE_STORE_URL")
    if not endpoint:
        return None
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.getenv("ABRL_S3_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("ABRL_S3_SECRET_KEY"),
        config=config,
    )


def release_proxy(db, row):
    if not row.proxy_lease_id:
        return
    lease = db.get(ProxyLease, row.proxy_lease_id)
    if lease.status == "active":
        lease.status = "released"
        lease.released_at = now()
        inventory = db.scalar(
            select(ProxyInventory).where(ProxyInventory.id == lease.inventory_id).with_for_update()
        )
        inventory.active_leases = max(0, inventory.active_leases - lease.config.get("max_connections", 1))


def record_proxy_transport_issue(db, row, attempt, transport_events):
    issue = next(
        (item for item in reversed(transport_events) if item.get("reason") == PROXY_DESTINATION_DENIED),
        None,
    ) or next(
        (item for item in reversed(transport_events) if item.get("reason") in PROXY_TRANSPORT_REASONS),
        None,
    )
    if issue is None:
        return
    detail = {"reason": issue["reason"], "attempt": attempt.number}
    status = issue.get("status")
    if type(status) is int and 100 <= status <= 599:
        detail["upstream_status"] = status
    event(db, row.project_id, "proxy_transport_issue", row.id, **detail)


def _retry_reason(work, result, attempt):
    if (
        work.mode != "live"
        or result.outcome != "failed"
        or not result.metadata.get("runtime_stopped")
        or result.metadata.get("needs_reconciliation")
        or result.metadata.get("non_idempotent_action_started")
        or result.metadata.get("profile_save_failed")
        or result.metadata.get("cleanup_error")
        or result.metadata.get("steps_completed", 0) != 0
    ):
        return None
    kind = result.metadata.get("failure_kind")
    if kind == "startup_transient" and not attempt.started_action:
        return kind
    if kind != "transport" or any(
        item.get("retryable") is False or item.get("kind") == "upstream_rejected"
        or item.get("reason") == PROXY_DESTINATION_DENIED
        for item in result.metadata.get("transport_events", [])
    ):
        return None
    policy = work.config.get("proxy_policy")
    if policy and (
        policy.get("failure_policy", "connection_recovery") != "connection_recovery"
        or attempt.number > policy.get("max_replacements", 1)
    ):
        return None
    transport = result.metadata.get("transport_summary") or {}
    attempts = transport.get("attempts", 0)
    if (
        work.config["target"]["origin"].startswith("https://")
        and attempts > 0
        and transport.get("successes") == 0
        and transport.get("failures") == attempts
    ):
        return kind  # No origin tunnel was established, so initial navigation sent no target request.
    return None


def _quarantine_runtime(db, row, profile, attempt):
    row.status = "needs_reconciliation" if attempt and attempt.started_action else "failed"
    row.error = "Worker lease expired; runtime shutdown must be confirmed before profile recovery"
    row.outcome = "failed"
    row.finished_at = now()
    row.admitted = False
    row.queue_reason = None
    row.next_attempt_at = None
    row.controller_id = None
    row.controller_until = None
    if attempt:
        attempt.outcome = row.status
        attempt.finished_at = row.finished_at
        attempt.error = row.error
    profile.status = "quarantined"
    event(db, row.project_id, "runtime_quarantined", row.id, worker=row.owner_worker, epoch=row.epoch)
    settle_batch(db, row.batch_id)


def _failure_detail(result, attempt):
    if result.outcome in {"completed", "cancelled"}:
        return None
    failure = result.metadata.get("causal_failure") or result.metadata.get("step_failure")
    if tunnel_failure := first_proxy_tunnel_failure(result.metadata, failure, result.elapsed_ms):
        return tunnel_failure
    if isinstance(failure, dict):
        return failure
    if result.metadata.get("profile_save_failed"):
        phase = "profile"
    elif result.metadata.get("cleanup_error") or not result.metadata.get("runtime_stopped"):
        phase = "cleanup"
    else:
        phase = "workflow" if attempt.started_action else "startup"
    return {
        "phase": phase,
        "reason": result.metadata.get("adapter_status") or result.metadata.get("failure_kind") or "attempt_failed",
        "elapsed_ms": result.elapsed_ms,
    }


def finish(work, result, saved=None, *, proxy_check_ip=None):
    from abrl.costs import record_attempt_usage

    with Session.begin() as db:
        row = db.scalar(select(BrowserSession).where(BrowserSession.id == work.id).with_for_update())
        profile = db.scalar(
            select(BrowserProfile).where(BrowserProfile.id == work.profile_id).with_for_update()
        )
        if (
            row is None
            or profile is None
            or row.lease_token != work.lease
            or row.epoch != work.epoch
            or profile.epoch != work.epoch
            or profile.owner_session_id != work.id
        ):
            return
        attempt = db.get(SessionAttempt, work.lease)
        if row.status not in ACTIVE or not row.lease_until or row.lease_until <= now():
            if row.status in ACTIVE:
                _quarantine_runtime(db, row, profile, attempt)
            if profile.status == "quarantined" and result.metadata.get("runtime_stopped") is True:
                if not row.metadata_json.get("late_shutdown_confirmed"):
                    row.metadata_json = {**row.metadata_json, "late_shutdown_confirmed": True}
                    event(db, row.project_id, "late_runtime_shutdown_confirmed", row.id, epoch=row.epoch)
            return
        stopping = row.status == "stopping"
        attempt.started_action = attempt.started_action or result.started_action
        if row.metadata_json.get("non_idempotent_action_attempt") == attempt.number:
            result.metadata["non_idempotent_action_started"] = True
            if result.outcome != "completed":
                result.metadata["needs_reconciliation"] = True
        row.status = "cancelled" if stopping else result.outcome
        if result.metadata.get("needs_reconciliation"):
            row.status = "needs_reconciliation"
        attempt.finished_at = now()
        attempt.outcome = row.status
        attempt.error = None if row.status == "cancelled" else redact_session_text(result.error)
        recording_status = result.metadata.get("recording_status")
        if recording_status in {"ready", "partial", "unavailable"}:
            attempt.recording_status = recording_status
        stopped = result.metadata.get("runtime_stopped", False) or work.mode == "demo"
        transport_events = result.metadata.get("transport_events", [])
        if transport_events:
            # Remote brokers can lag the session runner during a rolling upgrade.
            transport_events = [
                {key: value for key, value in issue.items() if key not in {"requested_authority", "upstream_authority"}}
                for issue in transport_events
            ]
            result.metadata["transport_events"] = transport_events
        proxy_config = None
        if row.proxy_lease_id:
            lease = db.get(ProxyLease, row.proxy_lease_id)
            proxy_config = lease.config
            inventory = db.scalar(
                select(ProxyInventory).where(ProxyInventory.id == lease.inventory_id).with_for_update()
            )
            if proxy_check_ip and result.outcome == "completed" and not stopping:
                attempt.proxy_check_ip = proxy_check_ip
            record_proxy_transport_issue(db, row, attempt, transport_events)
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
        row.metadata_json = redact_session_metadata({
            **{key: value for key, value in result.metadata.items() if key != "recording_status"},
            **public_transport_checks(result.metadata),
            "usage_attempts": usage_attempts,
            "input_profile_snapshot": work.snapshot,
            "environment_epoch": row.epoch,
            "participants": participants,
            "participant_id": participants[0] if len(participants) == 1 else None,
        })
        row.elapsed_ms = result.elapsed_ms
        row.error = attempt.error
        row.outcome = "cancelled" if row.status == "cancelled" else result.outcome
        row.workflow_outcome = result.metadata.get("workflow_outcome", result.outcome)
        row.finished_at = now()
        row.lease_until = None
        row.admitted = False
        row.queue_reason = None
        row.next_attempt_at = None
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
        retry_reason = _retry_reason(work, result, attempt) if row.status == "failed" else None
        failure = _failure_detail(result, attempt)
        attempt_detail = {"attempt": attempt.number, "outcome": attempt.outcome, "epoch": row.epoch}
        if (public_failure := public_lifecycle_failure(failure)) is not None:
            # Session events historically call the bounded HTTP status field "status".
            if (status := public_failure.pop("http_status")) is not None:
                public_failure["status"] = status
            attempt_detail["failure"] = {key: value for key, value in public_failure.items() if value is not None}
            row.metadata_json = {**row.metadata_json, "step_failure": attempt_detail["failure"]}
        if retry_reason and 1 <= attempt.number <= len(RETRY_DELAYS_SECONDS):
            if (elapsed := public_duration_ms(result.elapsed_ms)) is not None:
                attempt_detail["elapsed_ms"] = elapsed
            if (timeline := public_lifecycle_timeline(result.metadata.get("lifecycle_timeline"))) is not None:
                attempt_detail["lifecycle_timeline"] = timeline
            if (transport := public_transport_summary(result.metadata.get("transport_summary"))) is not None:
                attempt_detail["transport_summary"] = transport
            row.status = "queued"
            row.finished_at = None
            row.lease_token = None
            row.next_attempt_at = utc_after(RETRY_DELAYS_SECONDS[attempt.number - 1])
            row.queue_reason = "retry_backoff"
            if retry_reason == "transport":
                row.metadata_json = {**row.metadata_json, "pre_action_transport_failure_verified": True}
            event(db, row.project_id, "session_attempt_finished", row.id, **attempt_detail)
            event(
                db,
                row.project_id,
                "connection_recovery_queued" if retry_reason == "transport" else "startup_recovery_queued",
                row.id,
                attempt=attempt.number,
                previous_epoch=row.epoch,
                next_attempt_at=row.next_attempt_at,
            )
        else:
            event(
                db,
                row.project_id,
                "session_finished",
                row.id,
                **attempt_detail,
            )
        settle_batch(db, row.batch_id)


def reap_lost(db):
    for row in db.scalars(
        select(BrowserSession)
        .where(BrowserSession.status.in_(ACTIVE), BrowserSession.lease_until < now())
        .with_for_update(skip_locked=True)
    ):
        attempt = db.get(SessionAttempt, row.lease_token)
        profile = db.scalar(
            select(BrowserProfile).where(BrowserProfile.id == row.profile_id).with_for_update()
        )
        _quarantine_runtime(db, row, profile, attempt)


async def _publish_javascript_capture(work: Work, capture: JavaScriptCapture, result: BrowserResult) -> dict:
    provenance = {
        "session_id": work.id,
        "attempt_number": work.attempt_number,
        "epoch": work.epoch,
        "adapter": work.adapter,
        "browser_version": result.metadata.get("browser_version"),
        "target_version": work.config.get("target_version"),
        "workflow_version": work.config.get("workflow_version"),
    }
    status = {"status": "unavailable", "attempt_number": work.attempt_number}
    try:
        archive, summary = await asyncio.to_thread(capture.build_archive, provenance)
    except Exception:
        status["reason"] = "archive_failed"
        return status
    status.update(summary)
    try:
        status["artifact_id"] = await asyncio.to_thread(store_javascript_artifact, work, archive)
    except Exception:
        status.update(status="unavailable", reason="storage_unavailable")
    return status


async def _finalize_work(work, result, saved, proxy_check_ip):
    for delay in (0.05, 0.2, None):
        try:
            await asyncio.to_thread(finish, work, result, saved, proxy_check_ip=proxy_check_ip)
        except DBAPIError as exc:
            retryable = exc.connection_invalidated or getattr(exc.orig, "sqlstate", None) in {"40001", "40P01"}
            if delay is None or not retryable:
                raise
        except DatabaseTimeout:
            if delay is None:
                raise
        else:
            return
        await asyncio.sleep(delay)


def _checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _restore_profile(work, store, directory):
    snapshot = work.snapshot
    environment = snapshot["environment"]
    if environment.get("os") != platform.system() or environment.get("architecture") != platform.machine():
        raise ProfileStorageError("Persistent snapshot cannot cross operating-system or architecture boundaries")
    path = store.root / work.profile_id / f"{snapshot['generation']}.abrl"
    s3 = profile_s3() if not path.exists() else None
    checksum_error = "Snapshot ciphertext checksum does not match the immutable database manifest"
    if s3:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        pending = path.with_name(f".{path.name}.{work.lease}.pending")
        try:
            s3.download_file(os.getenv("ABRL_PROFILE_BUCKET", "abrl-profiles"), snapshot["key"], str(pending))
            if _checksum(pending) != snapshot["checksum"]:
                raise ProfileStorageError(checksum_error)
            pending.replace(path)
        finally:
            pending.unlink(missing_ok=True)
    else:
        try:
            actual_checksum = _checksum(path)
        except FileNotFoundError as exc:
            raise ProfileStorageError("The saved profile snapshot file is missing") from exc
        if actual_checksum != snapshot["checksum"]:
            raise ProfileStorageError(checksum_error)
    store.restore(work.profile_id, snapshot["generation"], directory, compatibility=environment)


def _profile_generation(work):
    with Session() as db:
        return f"{db.get(BrowserProfile, work.profile_id).version + 1}-{work.epoch}"


def _cleanup_work_directories(directory, recording_directory, stopped):
    with suppress(OSError):
        recording_directory.rmdir()
        recording_directory.parent.rmdir()
    if stopped:
        with suppress(FileNotFoundError):
            shutil.rmtree(directory)


@trace_session
async def execute(work):
    started = monotonic()
    directory = WORK_ROOT / work.id / work.lease
    recording_directory = WORK_ROOT / "recordings" / work.id / work.lease
    result = None
    saved = None
    proxy_check_ip = None
    browser_started = False
    preflight = None
    diagnostic = False
    javascript_capture = None
    javascript_publication = None
    timeline = LifecycleTimeline()

    lost = False
    lease_refresh = asyncio.Lock()

    async def refresh_lease():
        nonlocal lost
        async with lease_refresh:
            if not lost:
                try:
                    lost = await asyncio.to_thread(heartbeat, work)
                except Exception:
                    lost = True
            return lost

    async def keepalive():
        while not await refresh_lease():
            await asyncio.sleep(3)

    async def cancelled():
        return lost or STOPPING

    async def launch_cancelled():
        if await cancelled():
            return True
        await refresh_lease()
        return await cancelled()

    def stopped_before_launch():
        return BrowserResult(
            "cancelled", 0, None, {"runtime_stopped": True, "workflow_outcome": "not_started"}, False,
        )

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
                raise
            return
        await asyncio.to_thread(record_activity, work, payload)

    async def control():
        return await asyncio.to_thread(poll_control, work)

    async def recording_started():
        await asyncio.to_thread(record_activity, work, {"kind": "recording_started"})

    async def publish_recording_segment(segment):
        await asyncio.to_thread(store_recording_segment, work, segment)

    def observation_received():
        with Session() as db:
            observation_id = db.scalar(
                select(ProviderObservation.id).where(
                    ProviderObservation.session_id == work.id, ProviderObservation.attempt_id == work.lease
                )
            )
            return observation_id is not None

    async def wait_for_observation():
        with suppress(TimeoutError):
            async with asyncio.timeout(40):
                while not await asyncio.to_thread(observation_received):
                    await asyncio.sleep(0.5)
                return True
        return False

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
            target = work.config["target"]
            workflow = work.config["workflow"]
            diagnostic = bool(work.config.get("owned_diagnostic"))
            try:
                fixture_matches = is_managed_diagnostic(target["origin"], workflow)
            except ValueError:
                # Keep the managed redaction and stop before transport or browser launch.
                diagnostic, fixture_matches = True, False
            diagnostic = diagnostic or fixture_matches
            policy = work.config.get("proxy_policy")
            if diagnostic and policy and not await asyncio.to_thread(_diagnostic_proxy_ready, work, policy):
                reason = "diagnostic_proxy_binding_required"
            elif diagnostic and (not fixture_matches or not managed_diagnostic_configuration_valid(
                work.adapter, work.persistence, target,
            )):
                reason = "fixture_configuration"
            else:
                reason = None
            if reason:
                result = BrowserResult(
                    "blocked", 0, "Owned diagnostic assignment cannot start",
                    {
                        "runtime_stopped": True, "workflow_outcome": "not_started",
                        "causal_failure": {"phase": "diagnostic", "reason": reason, "elapsed_ms": 0},
                    }, False,
                )
                return
            if not diagnostic and work.config.get("capture_javascript") is True:
                javascript_capture = JavaScriptCapture()
            if work.proxy and work.proxy.get("provider") == "decodo":
                if await launch_cancelled():
                    result = stopped_before_launch()
                    return
                policy = work.config.get("proxy_policy") or {}
                try:
                    binding = ProxyBinding.from_config(work.proxy)
                except ProxyConfigurationError:
                    # Binding readiness is reported independently of a gateway credential rejection.
                    binding = None
                preflight = await run_decodo_preflight(
                    binding, work.config["target"]["origin"], policy, cancelled
                )
                timeline.mark("preflight_completed")
                preflight.update(version=1, attempt=work.attempt_number, epoch=work.epoch)
                await emit({"kind": "proxy_preflight", **preflight})
                stopping = preflight["status"] == "cancelled" or await launch_cancelled()
                if stopping or preflight["blocked"]:
                    reason = "cancelled" if stopping else preflight["reason"]
                    elapsed = preflight["timings_ms"].get("total", 0)
                    result = BrowserResult(
                        "cancelled" if stopping else "blocked", int(elapsed),
                        None if stopping else f"Decodo pre-flight blocked the launch ({reason})",
                        {
                            "runtime_stopped": True, "workflow_outcome": "not_started",
                            "failure_kind": "authentication" if reason == "authentication" else "preflight",
                            "causal_failure": {"phase": "preflight", "reason": reason, "elapsed_ms": elapsed},
                        }, False,
                    )
                    return
            if work.persistence == "persistent":
                try:
                    store = await asyncio.to_thread(snapshot_store)
                    if work.snapshot:
                        await asyncio.to_thread(_restore_profile, work, store, directory)
                    else:
                        await asyncio.to_thread(directory.mkdir, mode=0o700, parents=True, exist_ok=True)
                except Exception as exc:
                    elapsed = round((monotonic() - started) * 1000)
                    reason = "profile_restore_failed" if work.snapshot else "profile_store_unavailable"
                    timeline.mark("failure_observed")
                    result = BrowserResult(
                        "failed", elapsed,
                        str(exc) if isinstance(exc, ProfileStorageError) else "Persistent profile could not be prepared",
                        {
                            "runtime_stopped": True, "workflow_outcome": "not_started", "adapter_status": reason,
                            "causal_failure": {"phase": "profile", "reason": reason, "elapsed_ms": elapsed},
                        },
                        False,
                    )
                    return
            preparation = effective_preparation(work.adapter, None if diagnostic else work.config.get("preparation"))
            preparation["seed"] = work.config["seed"]
            steps = workflow["steps"]
            runtime_workflow = workflow
            if diagnostic:
                steps = diagnostic_steps()
                if (step_timeout := workflow["steps"][0].get("timeout_ms")) is not None:
                    steps[0]["timeout_ms"] = step_timeout
                runtime_workflow = {"initial_navigation_timeout_ms": workflow.get("initial_navigation_timeout_ms")}
            workflow_inputs = (
                work.config.get("workflow_inputs") if not diagnostic and workflow.get("schema_version") == 2 else None
            )
            proxy = work.proxy
            proxy_policy = work.config.get("proxy_policy") or {}
            policy_origin = proxy_policy.get("target_origin")
            if proxy and policy_origin:
                proxy = {**proxy, "target_origin": policy_origin}
            proxy_check = bool(proxy and target["mode"] == "browser_only" and not diagnostic)
            observation_context = None
            if work.config["target"]["mode"] == "instrumented":
                from abrl.observations import context_token

                observation_context = context_token(work.id, work.lease, work.epoch)
            timeout_seconds = work.config["timeout_seconds"]
            request_budget = work.config.get("request_budget", 1000)
            traffic_budget_bytes = work.config.get("traffic_budget_bytes", 104857600)
            if diagnostic:
                timeout_seconds = min(30, timeout_seconds)
                request_budget = min(20, request_budget)
                traffic_budget_bytes = min(1_048_576, traffic_budget_bytes)
            if await launch_cancelled():
                result = stopped_before_launch()
                return
            browser_started = True
            result = await execute_workflow(
                origin=work.config["target"]["origin"],
                navigation_origins=work.config["target"].get("navigation_origins", []),
                resource_origins=work.config["target"]["resource_origins"],
                steps=steps,
                cancel=cancelled,
                timeout_seconds=timeout_seconds,
                adapter=work.adapter,
                profile_dir=str(directory) if work.persistence == "persistent" else None,
                expected_compatibility=work.snapshot["environment"] if work.snapshot else None,
                preparation=preparation,
                proxy=proxy,
                proxy_navigation_timeout_ms=proxy_policy.get("initial_navigation_timeout_ms"),
                emit=emit,
                control=control,
                artifact_policy="none" if diagnostic else work.config.get("artifact_policy", "none"),
                request_budget=request_budget,
                traffic_budget_bytes=traffic_budget_bytes,
                user_agent=None if diagnostic else work.config.get("user_agent"),
                device_preset=work.config.get("device_preset"),
                referrer=None if diagnostic else work.config.get("referrer"),
                observation_context=observation_context,
                wait_for_observation=wait_for_observation,
                recording_directory=None if diagnostic else recording_directory,
                recording_started=recording_started,
                store_recording_segment=publish_recording_segment,
                javascript_capture=javascript_capture,
                lifecycle_timeline=timeline,
                owned_diagnostic=diagnostic,
                workflow=runtime_workflow,
                workflow_inputs=workflow_inputs,
                **({"batch_preset": work.config.get("batch_preset") or {}} if work.config.get("batch_presets") else {}),
            )
            if javascript_capture is not None:
                javascript_publication = asyncio.create_task(
                    _publish_javascript_capture(work, javascript_capture, result)
                )
            if (
                proxy_check
                and result.outcome == "completed"
                and result.metadata.get("runtime_stopped")
                and not await cancelled()
            ):
                # A standalone diagnostic cannot change a run's outcome or cleanup.
                with suppress(Exception):
                    if not await refresh_lease() and not STOPPING:
                        proxy_check_ip = await check_proxy_ip(ProxyBinding.from_config(work.proxy))
            if (
                work.persistence == "persistent"
                and result.metadata.get("runtime_stopped")
                and result.metadata.get("profile_saved")
                and not result.metadata.get("cleanup_error")
            ):
                try:
                    generation = await asyncio.to_thread(_profile_generation, work)
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
        result = BrowserResult("cancelled", 0, "Runner stopping", {"runtime_stopped": not browser_started}, False)
        raise
    except Exception as exc:
        log.warning("Session launch/persistence failed: %s", type(exc).__name__)
        if result:
            result.outcome = "failed"
            result.error = "Browser runtime termination could not be confirmed"
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
        try:
            if result is not None and (recorded_timeline := timeline.view()) is not None:
                result.metadata["lifecycle_timeline"] = recorded_timeline
            if result is not None and diagnostic:
                if result.outcome in {"failed", "blocked"}:
                    result.error = "Owned browser diagnostics did not complete"
                preset = preset_details(work.config.get("batch_preset"))
                preset_timezone = preset["timezone"] if preset else None
                expected_timezone = preset_timezone or confirmed_sticky_timezone(preflight)
                timezone_source = "batch_preset" if preset_timezone else "sticky_preflight" if expected_timezone else None
                try:
                    report = diagnostic_report(
                        result, adapter=work.adapter, device_preset=work.config.get("device_preset"),
                        preflight=preflight, expected_timezone=expected_timezone,
                        timezone_source=timezone_source,
                        expected_language=preset["locale"] if preset else None,
                        expected_dpr=preset["device_pixel_ratio"] if preset else None,
                        retain_observations=False, fixture_origin=work.config["target"]["origin"],
                    )
                    report.pop("preflight", None)
                    result.metadata["browser_diagnostic"] = {
                        **report, "version": 1, "attempt": work.attempt_number, "epoch": work.epoch,
                    }
                finally:
                    for key in ("extractions", "actual_user_agent", "vendor_profile_id", "cdp_endpoint"):
                        result.metadata.pop(key, None)
            if result is not None and preflight is not None:
                result.metadata["proxy_preflight"] = preflight
            if browser_started and javascript_capture is not None and result is not None:
                if javascript_publication is None:
                    javascript_publication = asyncio.create_task(
                        _publish_javascript_capture(work, javascript_capture, result)
                    )
                try:
                    result.metadata["javascript_capture"] = await asyncio.shield(javascript_publication)
                except asyncio.CancelledError:
                    result.metadata["javascript_capture"] = await javascript_publication
                    raise
        finally:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task
            try:
                if result:
                    await _finalize_work(work, result, saved, proxy_check_ip)
            finally:
                try:
                    await asyncio.to_thread(
                        _cleanup_work_directories, directory, recording_directory,
                        bool(result and result.metadata.get("runtime_stopped")),
                    )
                except OSError:
                    log.warning("Session work directory cleanup failed")


def fleet_touch(pool, capacity):
    from abrl.platform_service import execution_capabilities
    from abrl.recording import recording_encoder_available

    supported = sorted(
        entry["id"] for entry in execution_capabilities() if entry["available"] and adapter_pool(entry["id"]) == pool
    )
    metadata = {
        "node_name": socket.gethostname(),
        "recording_encoder": recording_encoder_available(),
        "adapters": supported,
    }
    with Session.begin() as db:
        entry = db.get(FleetSlot, WORKER)
        if entry:
            entry.seen_at = now()
            entry.capacity = capacity
            entry.metadata_json = {**entry.metadata_json, **metadata}
        else:
            db.add(
                FleetSlot(
                    id=WORKER,
                    pool=pool,
                    capacity=capacity,
                    seen_at=now(),
                    metadata_json=metadata,
                )
            )
    return supported


def _admit_one_shot_worker():
    with Session.begin() as db:
        admission_tick(db, require_worker_heartbeat=True)


async def _execute_in_worker(work):
    try:
        await execute(work)
    except Exception as exc:
        # The lease reaper handles unfinished attempts when finalization fails.
        log.error("Session %s runner failed: %s", work.id, type(exc).__name__)


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
            tasks = {task for task in tasks if not task.done()}
            try:
                supported = await asyncio.to_thread(fleet_touch, pool, capacity)
                if once:
                    await asyncio.to_thread(_admit_one_shot_worker)
                while not STOPPING and len(tasks) < capacity:
                    work = await asyncio.to_thread(claim, pool, supported_adapters=supported)
                    if not work:
                        break
                    if once:
                        await execute(work)
                        return
                    tasks.add(asyncio.create_task(_execute_in_worker(work)))
            except (DBAPIError, DatabaseTimeout) as exc:
                if once:
                    raise
                log.warning("Session worker could not poll for work: %s", type(exc).__name__)
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
