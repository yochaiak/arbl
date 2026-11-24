import hashlib
import json
import random
import secrets
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

from fastapi import HTTPException
from sqlalchemy import func, select

from abrl.batch_presets import MAX_BATCH_SEED, preset_details, select_preset
from abrl.browser_diagnostics import is_managed_diagnostic
from abrl.browser_settings import (
    adapter_contract,
    adapter_pool,
    operator_controlled,
    supports_user_agent_override,
)
from abrl.config import CONTROLLED_ORIGIN
from abrl.connect_telemetry import public_navigation_connects
from abrl.db import Target, TargetVersion, Workflow, WorkflowVersion, now
from abrl.network import FIXTURE_ORIGIN, is_fixture_path, request_origin
from abrl.platform_models import (
    BrowserProfile,
    BrowserSession,
    FleetSlot,
    ProviderObservation,
    ProxyPolicy,
    SessionArtifact,
    SessionAttempt,
    SessionBatch,
    SessionBatchItem,
    SessionEvent,
    SessionTemplate,
    SessionTemplateRevision,
)
from abrl.platform_schemas import BatchInput
from abrl.proxy import proxy_scope_allows_target, select_proxy
from abrl.schemas import (
    ASSERTIONS,
    FixtureParameterName,
    FixtureParams,
    resolve_navigation_parameters,
    resolve_workflow_inputs,
    validate_navigation_workflow_scope,
    walk_workflow_steps,
    workflow_parameter_names,
)
from abrl.session_metadata import (
    current_viewport_conformance,
    public_session_event_detail,
    redact_session_metadata,
    redact_session_text,
)
from abrl.target_authorization import LIVE_SITE_APPROVAL_REQUIRED, target_is_live_ready

ACTIVE = ("starting", "preparing", "ready", "running", "awaiting_operator", "stopping")
TERMINAL = ("completed", "failed", "blocked", "cancelled", "needs_reconciliation")
GLOBAL_LIMIT = 500


def utc_after(seconds):
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def live_worker_capacity(db):
    """Report pool capacity and adapter-specific capacity where workers publish it."""
    pools, adapters = Counter(), Counter()
    reported_pools = set()
    for pool, capacity, metadata in db.execute(
        select(FleetSlot.pool, FleetSlot.capacity, FleetSlot.metadata_json)
        .where(FleetSlot.seen_at > utc_after(-30))
    ):
        pools[pool] += capacity
        supported = (metadata or {}).get("adapters")
        if isinstance(supported, list):
            reported_pools.add(pool)
            for adapter in {entry for entry in supported if isinstance(entry, str)}:
                if (contract := adapter_contract(adapter)) and contract.pool == pool:
                    adapters[adapter] += capacity
    return pools, adapters, reported_pools


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def event(db, project_id, kind, session_id=None, actor_id=None, **detail):
    db.add(
        SessionEvent(
            project_id=project_id, session_id=session_id, kind=kind, actor_id=actor_id,
            detail=public_session_event_detail(kind, detail),
        )
    )


def _require_navigation_scope(steps, target, *, all_navigation=False):
    try:
        validate_navigation_workflow_scope(
            steps,
            origin=target["origin"],
            resource_origins=target.get("resource_origins", []),
            navigation_origins=target.get("navigation_origins", []),
            all_navigation=all_navigation,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _require_proxy_scope(policy, target):
    if policy and not proxy_scope_allows_target(policy.get("target_origin"), target):
        raise HTTPException(409, "The proxy policy requires its configured origin as the sole site origin")


def uses_retired_lab_fixture(target: dict, workflow: dict) -> bool:
    origin = target["origin"]
    if origin == FIXTURE_ORIGIN:
        return True
    if not CONTROLLED_ORIGIN or origin != CONTROLLED_ORIGIN:
        return False
    for step in walk_workflow_steps(workflow.get("steps", [])):
        if step.get("action") in {"navigate", "open_tab"} and step.get("url"):
            url = urljoin(origin + "/", step["url"])
            if is_fixture_path(url) and request_origin(url) == origin:
                return True
    return False


def batch_fixture_parameter_names(config: dict) -> list[FixtureParameterName]:
    steps = config["workflow"].get("steps", [])
    _require_navigation_scope(steps, config["target"], all_navigation=config["workflow"].get("schema_version") == 2)
    return workflow_parameter_names(steps)


def _merge_launch_fixture_params(config: dict, values: FixtureParams) -> dict:
    unused = values.keys() - set(batch_fixture_parameter_names(config))
    if unused:
        raise ValueError(f"Workflow does not use these URL parameters: {', '.join(sorted(unused))}")
    workflow = deepcopy(config["workflow"])
    for step in walk_workflow_steps(workflow["steps"]):
        defaults = step.get("fixture_params")
        if step.get("action") in {"navigate", "open_tab"} and defaults:
            parameters = {name: values.get(name, default) for name, default in defaults.items()}
            resolve_navigation_parameters(step["url"], parameters)
            step["fixture_params"] = parameters
    return {**config, "fixture_params": dict(values), "workflow": workflow}


def _load_policy(db, policy_id, project_id, target):
    if not policy_id:
        return None
    row = db.get(ProxyPolicy, policy_id)
    if not row or row.project_id != project_id:
        raise HTTPException(404, "Proxy policy not found")
    policy = {**row.config, "id": row.id, "version": row.version}
    _require_proxy_scope(policy, target)
    return policy


def _resolve_launch_inputs(config, values):
    try:
        return resolve_workflow_inputs(config["workflow"], values)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _launch_options(db, data, config, project_id, *, frozen_policies=None):
    config = _with_launch_parameters(config, data.fixture_params)
    try:
        diagnostic = is_managed_diagnostic(config["target"]["origin"], config["workflow"])
    except ValueError as exc:
        raise HTTPException(409, "The owned diagnostic fixture configuration is invalid") from exc
    if config.get("owned_diagnostic") and not diagnostic:
        raise HTTPException(409, "The pinned diagnostic fixture no longer matches the configured origin")
    if diagnostic:
        config = {**config, "owned_diagnostic": True}
    if data.mode == "live":
        _require_operator_workflow(data, config)
    if not data.session_groups:
        config["workflow_inputs"] = _resolve_launch_inputs(config, data.workflow_inputs)
        return config
    groups = []
    policies = {} if frozen_policies is None else frozen_policies.copy()
    for group in data.session_groups:
        policy_id = group.proxy_policy_id
        if policy_id and policy_id not in policies:
            policies[policy_id] = _load_policy(db, policy_id, project_id, config["target"])
        base = _with_launch_parameters(config, group.fixture_params)
        user_agent = group.user_agent or data.user_agent
        if user_agent and not supports_user_agent_override(group.adapter):
            raise HTTPException(422, f"{group.adapter} does not support a User-Agent override in this group")
        groups.append({
            "group_fixture_params": group.fixture_params,
            "workflow_inputs": _resolve_launch_inputs(base, {**data.workflow_inputs, **group.workflow_inputs}),
            "proxy_policy": None if group.use_direct else policies.get(policy_id, config.get("proxy_policy")),
            "device_preset": group.device_preset or data.device_preset,
            "user_agent": user_agent,
            "persistence": group.persistence or data.persistence,
        })
    return {**config, "group_launch_configs": groups}


def select_session_proxy(config, policy, adapter, inventory):
    binding_policy = {**policy, "duration_minutes": policy["continuity_minutes"], "adapter": adapter}
    if policy.get("allow_direct"):
        try:
            diagnostic = config.get("owned_diagnostic") or is_managed_diagnostic(
                config["target"]["origin"], config["workflow"]
            )
        except ValueError:
            # Older snapshots have no pinned diagnostic marker. Invalid fixture
            # configuration cannot justify direct egress for a proxy measurement.
            diagnostic = True
        if diagnostic:
            binding_policy["allow_direct"] = False
    return select_proxy(binding_policy, inventory)


def batch_snapshot(db, data: BatchInput, project_id: str) -> dict:
    requested = data.model_dump(mode="json")
    if data.session_template_id:
        template = db.get(SessionTemplate, data.session_template_id)
        revision = db.get(
            SessionTemplateRevision, (data.session_template_id, data.session_template_version)
        )
        if not template or template.project_id != project_id or not revision:
            raise HTTPException(404, "Session template version not found")
        pinned = revision.config["pinned"]
        # Name and request identity are launch-specific. Other changes require a
        # new template revision or an explicitly untemplated launch.
        ignored = {"name", "idempotency_key", "session_template_id", "session_template_version"}
        launch_fields = {"fixture_params", "expected_workflow_version", "capture_javascript", "expected_dependencies"}
        baseline = BatchInput.model_validate(revision.config["batch"]).model_dump(mode="json")
        if any(requested[key] != baseline[key] for key in baseline.keys() - ignored - launch_fields):
            raise HTTPException(409, "Launch configuration differs from the pinned session template")
        config = {
            **pinned,
            **{key: requested[key] for key in ignored},
            "capture_javascript": data.capture_javascript,
        }
        _validate_pinned_batch(db, data, config)
        frozen_policies = {p["id"]: p for group in pinned.get("group_launch_configs", []) if (p := group.get("proxy_policy"))}
        return _launch_options(db, data, config, project_id, frozen_policies=frozen_policies)
    target = db.get(Target, data.target_id)
    workflow = db.get(Workflow, data.workflow_id)
    if not target or not workflow:
        raise HTTPException(404, "Target or workflow not found")
    if (
        getattr(target, "project_id", "default") != project_id
        or getattr(workflow, "project_id", "default") != project_id
    ):
        raise HTTPException(404, "Target or workflow not found in project")
    target_config = db.get(TargetVersion, (target.id, target.version)).config
    if data.mode == "live" and not target_is_live_ready(target, target.version, target_config):
        raise HTTPException(409, LIVE_SITE_APPROVAL_REQUIRED)
    workflow_version = workflow.version
    if data.expected_workflow_version not in (None, workflow_version):
        raise HTTPException(409, "The workflow changed. Review the latest version before launching.")
    workflow_config = db.get(WorkflowVersion, (workflow.id, workflow_version)).config
    _require_navigation_scope(workflow_config.get("steps", []), target_config, all_navigation=workflow_config.get("schema_version") == 2)
    policy = _load_policy(db, data.proxy_policy_id, project_id, target_config)
    config = {
        **requested,
        "target": target_config,
        "target_version": target.version,
        "workflow": workflow_config,
        "workflow_version": workflow_version,
        "proxy_policy": policy,
    }
    return _launch_options(db, data, config, project_id)


def _with_launch_parameters(config: dict, values: FixtureParams | None) -> dict:
    if values is None:
        return config
    try:
        return _merge_launch_fixture_params(config, values)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


def _validate_pinned_batch(db, data: BatchInput, pinned: dict) -> None:
    if data.expected_workflow_version not in (None, pinned["workflow_version"]):
        raise HTTPException(409, "The workflow changed. Review the latest version before launching.")
    if data.mode == "live":
        target = db.get(Target, pinned["target_id"])
        if not target_is_live_ready(target, pinned["target_version"], pinned["target"]):
            raise HTTPException(409, LIVE_SITE_APPROVAL_REQUIRED)
    _require_navigation_scope(pinned["workflow"].get("steps", []), pinned["target"], all_navigation=pinned["workflow"].get("schema_version") == 2)
    _require_proxy_scope(pinned.get("proxy_policy"), pinned["target"])


def manual_success_check(workflow):
    return workflow.get("success_check") if workflow.get("schema_version", 1) == 2 else any(
        step["action"] in ASSERTIONS for step in workflow["steps"]
    )


def _require_operator_workflow(data: BatchInput, config: dict) -> None:
    if not all(map(operator_controlled, data.adapters)):
        return
    if config.get("owned_diagnostic"):
        raise HTTPException(409, "Managed diagnostics need a fresh automated session on the owned fixture")
    if not manual_success_check(config["workflow"]):
        raise HTTPException(409, "Manual Chrome requires a workflow with a success assertion")


def managed_diagnostic_configuration_valid(adapter, persistence, target) -> bool:
    contract = adapter_contract(adapter)
    return bool(
        contract and contract.features["managed_diagnostics"] and persistence == "fresh"
        and target["mode"] == "browser_only" and not target.get("resource_origins")
        and not target.get("navigation_origins")
    )


def effective_persistence(settings, profile=None):
    return profile.persistence if profile is not None else settings["persistence"]


def dependency_versions(config):
    policies = [config.get("proxy_policy"), *(group.get("proxy_policy") for group in config.get("group_launch_configs", []))]
    return {
        "target_id": config["target_id"], "target_version": config["target_version"],
        "workflow_id": config["workflow_id"], "workflow_version": config["workflow_version"],
        "proxy_policies": {policy["id"]: policy["version"] for policy in policies if policy},
    }


def require_live_batch(data: BatchInput) -> None:
    if data.mode != "live":
        raise HTTPException(422, "New session batches require live execution")
    if not data.session_template_id and data.preparation.mode != "none":
        raise HTTPException(422, "Preparation settings are unavailable for new batches")


def _legacy_batch_hash(data: BatchInput, payload: dict) -> str | None:
    if (
        data.batch_presets is not None or data.device_preset is not None
        or data.expected_workflow_version is not None or data.expected_dependencies
        or data.workflow_inputs or data.session_groups
    ):
        return None
    previous = payload.copy()
    if "capture_javascript" not in data.model_fields_set or data.capture_javascript is False:
        previous.pop("capture_javascript", None)
    if data.seed is None:
        previous["seed"] = 42
    for key, default in (("mode", "demo"), ("artifact_policy", "none"), ("name", "Browser experiment")):
        if key not in data.model_fields_set:
            previous[key] = default
    return digest(previous)


def create_batch(
    db,
    data: BatchInput,
    project_id: str,
    *,
    pinned: dict | None = None,
    actor_id: str | None = None,
    require_live: bool = False,
    allow_test_fixture: bool = False,
) -> SessionBatch:
    payload = data.model_dump(mode="json")
    # Unset launch settings must match fingerprints stored before they were introduced.
    for key in (
        "user_agent", "referrer", "fixture_params", "device_preset", "batch_presets",
        "expected_workflow_version", "expected_dependencies", "workflow_inputs", "session_groups",
    ):
        if payload[key] is None or payload[key] in ({}, []):
            del payload[key]
    request_hash = digest(payload)
    prior = db.scalar(
        select(SessionBatch).where(
            SessionBatch.project_id == project_id, SessionBatch.idempotency_key == data.idempotency_key
        )
    )
    if prior:
        if prior.request_hash not in (request_hash, _legacy_batch_hash(data, payload)):
            raise HTTPException(409, "This idempotency key belongs to different input")
        return prior
    if require_live:
        require_live_batch(data)
    config = pinned if pinned is not None else batch_snapshot(db, data, project_id)
    if pinned is not None:
        _validate_pinned_batch(db, data, pinned)
        if data.mode == "live":
            _require_operator_workflow(data, pinned)
    if require_live and not allow_test_fixture and uses_retired_lab_fixture(config["target"], config["workflow"]):
        raise HTTPException(409, "The Lab site was retired. Select an authorized site.")
    if data.expected_dependencies and data.expected_dependencies != digest(dependency_versions(config)):
        raise HTTPException(409, "Site, workflow, or proxy policy changed. Refresh batch readiness before launching.")
    seed = data.seed if data.seed is not None else secrets.randbelow(MAX_BATCH_SEED + 1)
    config = {**config, "seed": seed, "idempotency_key": data.idempotency_key}
    batch = SessionBatch(
        project_id=project_id,
        name=data.name,
        idempotency_key=data.idempotency_key,
        request_hash=request_hash,
        config=config,
        count=data.count,
        concurrency=data.concurrency,
    )
    db.add(batch)
    db.flush()
    if data.session_groups:
        allocations = [
            (group.adapter, group.profile_ids[index] if index < len(group.profile_ids) else None, settings)
            for group, settings in zip(data.session_groups, config["group_launch_configs"], strict=True)
            for index in range(group.count)
        ]
    else:
        allocations = [
            (adapter, data.profile_ids[index] if data.profile_ids else None, None)
            for index, adapter in enumerate(batch_allocation_adapters(db, data, project_id, seed))
        ]
    adapter_ordinals = Counter()
    for ordinal, (adapter, profile_id, settings) in enumerate(allocations, start=1):
        if data.batch_presets is not None:
            contract = adapter_contract(adapter)
            adapter_ordinals[adapter] += 1
            settings = {
                **(settings or {}),
                "batch_preset": select_preset(
                    data.batch_presets, ordinal=adapter_ordinals[adapter], seed=seed,
                    application=contract.features["batch_preset_application"],
                ),
            }
        item = SessionBatchItem(
            batch_id=batch.id,
            ordinal=ordinal,
            adapter=adapter,
            requested_profile_id=profile_id,
            launch_config=settings,
        )
        db.add(item)
        create_batch_item(db, batch, item, data)
    event(
        db,
        project_id,
        "batch_created",
        actor_id=actor_id,
        batch_id=batch.id,
        count=data.count,
        mode=data.mode,
    )
    db.flush()
    settle_batch(db, batch.id)
    return batch


def session_launch_config(db, row, batch=None, *, item=None, resolve_workflow=True):
    batch = batch if batch is not None else db.get(SessionBatch, row.batch_id)
    if not (batch.config.get("session_groups") or batch.config.get("batch_presets")):
        return batch.config
    if item is None:
        item = db.get(SessionBatchItem, (row.batch_id, row.ordinal))
    config = {**batch.config, **(item.launch_config if item and item.launch_config else {})}
    overrides = config.pop("group_fixture_params", None)
    return _with_launch_parameters(config, overrides) if resolve_workflow and overrides else config


def batch_allocation_adapters(db, data: BatchInput, project_id: str, seed: int) -> list[str]:
    adapters = [data.adapters[i % len(data.adapters)] for i in range(data.count)]
    random.Random(seed).shuffle(adapters)
    if not data.profile_ids:
        return adapters
    profiles = db.execute(
        select(BrowserProfile.id, BrowserProfile.adapter).where(
            BrowserProfile.project_id == project_id,
            BrowserProfile.id.in_(data.profile_ids),
        )
    )
    profile_adapters = dict(profiles.all())
    remaining = Counter(adapters)
    assigned = {}
    for index, profile_id in enumerate(data.profile_ids):
        adapter = profile_adapters.get(profile_id)
        if adapter is not None and remaining[adapter]:
            assigned[index] = adapter
            remaining[adapter] -= 1
    available = []
    for adapter in adapters:
        if remaining[adapter]:
            available.append(adapter)
            remaining[adapter] -= 1
    unassigned = iter(available)
    return [assigned[index] if index in assigned else next(unassigned) for index in range(data.count)]


def create_batch_item(db, batch, item, data):
    """Retry only allocation; a session that ever existed is never recreated."""
    if item.session_id is not None or item.status == "cancelled":
        return
    item.attempts = (item.attempts or 0) + 1
    persistence = (item.launch_config or {}).get("persistence", data.persistence)
    if item.requested_profile_id:
        profile = db.scalar(
            select(BrowserProfile).where(BrowserProfile.id == item.requested_profile_id).with_for_update()
        )
        if not profile or profile.project_id != batch.project_id or profile.adapter != item.adapter:
            item.status = "failed"
            item.error = "Selected profile is unavailable or incompatible with assigned adapter"
            return
        if profile.status != "available" or db.scalar(
            select(BrowserSession.id)
            .where(
                BrowserSession.profile_id == profile.id,
                BrowserSession.status.not_in(TERMINAL),
            )
            .limit(1)
        ):
            item.status = "failed"
            item.error = "Selected profile is active, reserved or quarantined"
            return
        persistence = effective_persistence({"persistence": persistence}, profile)
        item.launch_config = {**(item.launch_config or {}), "persistence": persistence}
    else:
        profile = BrowserProfile(
            project_id=batch.project_id,
            name=f"{data.name} · {item.ordinal}",
            adapter=item.adapter,
            persistence=persistence,
        )
        db.add(profile)
        db.flush()
    session = BrowserSession(
        project_id=batch.project_id,
        batch_id=batch.id,
        profile_id=profile.id,
        adapter=item.adapter,
        persistence=persistence,
        mode=data.mode,
        pool=adapter_pool(item.adapter),
        ordinal=item.ordinal,
        ground_truth="human"
        if operator_controlled(item.adapter)
        else "mixed"
        if data.preparation.mode == "manual"
        else "automated",
    )
    db.add(session)
    db.flush()
    item.session_id, item.status, item.error = session.id, "created", None


def batch_item_view(row):
    return {
        key: getattr(row, key)
        for key in (
            "batch_id",
            "ordinal",
            "adapter",
            "requested_profile_id",
            "session_id",
            "status",
            "error",
            "attempts",
            "created_at",
        )
    }


def profile_view(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "project_id",
            "name",
            "adapter",
            "persistence",
            "status",
            "version",
            "epoch",
            "owner_session_id",
            "created_at",
        )
    }


def session_view(row):
    metadata = redact_session_metadata(row.metadata_json)
    for private in ("navigation_connects", "batch_preset", "viewport_conformance", "viewport_conformance_epoch"):
        metadata.pop(private, None)
    if row.attempt_number > 0 and (conformance := current_viewport_conformance(row.metadata_json, row.epoch)) is not None:
        metadata["viewport_conformance"] = conformance
    epoch = metadata.get("environment_epoch")
    if type(epoch) is int and epoch == row.epoch:
        if (navigation_connects := public_navigation_connects(row.metadata_json.get("navigation_connects"))) is not None:
            metadata["navigation_connects"] = navigation_connects
    return {
        **{
            k: getattr(row, k)
            for k in (
                "id",
                "project_id",
                "profile_id",
                "batch_id",
                "adapter",
                "persistence",
                "status",
                "mode",
                "pool",
                "outcome",
                "workflow_outcome",
                "elapsed_ms",
                "ordinal",
                "admitted",
                "queue_reason",
                "next_attempt_at",
                "ground_truth",
                "epoch",
                "attempt_number",
                "controller_id",
                "controller_until",
                "created_at",
                "started_at",
                "finished_at",
            )
        },
        "error": redact_session_text(row.error),
        "metadata": metadata,
    }


def include_session_context(db, project_id, items):
    if not items:
        return
    context = {
        session_id: (name, version, assignment)
        for session_id, name, version, assignment in db.execute(
            select(
                BrowserSession.id,
                SessionBatch.config["workflow"]["name"],
                SessionBatch.config["workflow_version"],
                SessionBatchItem.launch_config["batch_preset"],
            ).join(SessionBatch, SessionBatch.id == BrowserSession.batch_id)
            .outerjoin(
                SessionBatchItem,
                (SessionBatchItem.session_id == BrowserSession.id) & (SessionBatchItem.batch_id == BrowserSession.batch_id),
            )
            .where(
                SessionBatch.project_id == project_id, BrowserSession.project_id == project_id,
                BrowserSession.id.in_({item["id"] for item in items}),
            )
        )
    }
    for item in items:
        name, version, assignment = context.get(item["id"], (None, None, None))
        item["workflow_name"] = name if isinstance(name, str) else None
        item["workflow_version"] = version if type(version) is int else None
        item["batch_preset"] = preset_details(assignment)


def session_view_with_workflow(db, row):
    item = session_view(row)
    include_session_context(db, row.project_id, [item])
    return item


def _current_attempts(project_id, *columns):
    return (
        select(*columns)
        .select_from(BrowserSession)
        .join(
            SessionAttempt,
            (SessionAttempt.session_id == BrowserSession.id)
            & (SessionAttempt.number == BrowserSession.attempt_number),
        )
        .where(BrowserSession.project_id == project_id)
    )


def _current_attempt_addresses(project_id):
    return _current_attempts(
        project_id, BrowserSession.id, BrowserSession.attempt_number, ProviderObservation.observed_ip
    ).outerjoin(
        ProviderObservation,
        (ProviderObservation.attempt_id == SessionAttempt.id)
        & (ProviderObservation.session_id == BrowserSession.id)
        & (ProviderObservation.project_id == BrowserSession.project_id),
    )


def include_session_ips(db, project_id, items):
    if not items:
        return
    addresses = {
        (session_id, attempt_number): (observed_ip, proxy_check_ip)
        for session_id, attempt_number, observed_ip, proxy_check_ip in db.execute(
            _current_attempt_addresses(project_id)
            .add_columns(SessionAttempt.proxy_check_ip)
            .where(BrowserSession.id.in_([item["id"] for item in items]))
        )
    }
    for item in items:
        item["observed_ip"], item["proxy_check_ip"] = addresses.get(
            (item["id"], item["attempt_number"]), (None, None)
        )


def recording_status(attempt, *, has_segments, has_retained_segments):
    if attempt.finished_at is None:
        return "recording" if has_segments or attempt.recording_status == "recording" else "unavailable"
    if has_retained_segments:
        return "ready" if attempt.recording_status == "ready" else "partial"
    return "expired" if has_segments else "unavailable"


def include_session_recordings(db, project_id, items):
    if not items:
        return
    by_id = {item["id"]: item for item in items}
    segments = select(SessionArtifact.id).where(
        SessionArtifact.attempt_id == SessionAttempt.id,
        SessionArtifact.session_id == SessionAttempt.session_id,
        SessionArtifact.project_id == project_id,
    )
    retained = segments.where(SessionArtifact.deleted_at.is_(None), SessionArtifact.expires_at > now())
    attempts = (
        select(SessionAttempt, segments.exists(), retained.exists())
        .join(BrowserSession, BrowserSession.id == SessionAttempt.session_id)
        .where(BrowserSession.project_id == project_id, BrowserSession.id.in_(by_id))
    )
    for attempt, has_segments, has_retained_segments in db.execute(attempts):
        item = by_id[attempt.session_id]
        if attempt.number == item["attempt_number"]:
            item["recording_status"] = recording_status(
                attempt, has_segments=has_segments, has_retained_segments=has_retained_segments
            )
        if attempt.finished_at is not None and has_retained_segments:
            item["recording_available"] = True


def batch_ip_summary(db, batch):
    addresses = (
        _current_attempt_addresses(batch.project_id)
        .where(BrowserSession.batch_id == batch.id, ProviderObservation.observed_ip.is_not(None))
        .subquery()
    )
    observed, distinct = db.execute(
        select(func.count(), func.count(func.distinct(addresses.c.observed_ip))).select_from(addresses)
    ).one()
    if distinct < observed:
        status = "duplicate"
    elif batch.status == "completed" and observed == batch.count:
        status = "unique"
    elif batch.status in {"completed", "cancelled", "creation_failed"}:
        status = "incomplete"
    else:
        status = "pending"
    return {
        "expected_sessions": batch.count,
        "observed_sessions": observed,
        "distinct_ips": distinct,
        "status": status,
    }


def batch_proxy_check_ip_summary(db, batch, *, show_ips):
    checked_sessions = _current_attempts(
        batch.project_id, BrowserSession.id, BrowserSession.ordinal, SessionAttempt.proxy_check_ip
    ).where(BrowserSession.batch_id == batch.id, SessionAttempt.proxy_check_ip.is_not(None))
    sessions_by_ip = defaultdict(list)
    for session_id, ordinal, address in db.execute(checked_sessions.order_by(BrowserSession.ordinal)):
        sessions_by_ip[address].append({"id": session_id, "ordinal": ordinal})

    duplicate_groups = [
        {"proxy_check_ip": address if show_ips else None, "sessions": sessions}
        for address, sessions in sessions_by_ip.items()
        if len(sessions) > 1
    ]
    unique_count = len(sessions_by_ip) - len(duplicate_groups)
    duplicate_count = sum(len(group["sessions"]) for group in duplicate_groups)
    return {
        "expected_sessions": batch.count,
        "checked_sessions": unique_count + duplicate_count,
        "distinct_ips": len(sessions_by_ip),
        "unique_sessions": unique_count,
        "duplicate_sessions": duplicate_count,
        "duplicate_groups": duplicate_groups,
    }


def batch_view(db, row):
    counts = dict(
        db.execute(
            select(BrowserSession.status, func.count())
            .where(BrowserSession.batch_id == row.id)
            .group_by(BrowserSession.status)
        ).all()
    )
    return {
        **{
            k: getattr(row, k)
            for k in (
                "id",
                "project_id",
                "name",
                "status",
                "count",
                "concurrency",
                "created_at",
                "finished_at",
            )
        },
        "mode": row.config["mode"],
        "config": row.config,
        "counts": counts,
        "creation": dict(
            db.execute(
                select(SessionBatchItem.status, func.count())
                .where(SessionBatchItem.batch_id == row.id)
                .group_by(SessionBatchItem.status)
            ).all()
        ),
    }


def settle_batch(db, batch_id):
    db.flush()
    row = db.get(SessionBatch, batch_id)
    statuses = list(db.scalars(select(BrowserSession.status).where(BrowserSession.batch_id == batch_id)))
    creation_failed = (
        db.scalar(
            select(SessionBatchItem.ordinal)
            .where(
                SessionBatchItem.batch_id == batch_id,
                SessionBatchItem.status == "failed",
            )
            .limit(1)
        )
        is not None
    )
    if all(s in TERMINAL for s in statuses):
        row.status = (
            "creation_failed"
            if creation_failed
            else ("cancelled" if not statuses or all(s == "cancelled" for s in statuses) else "completed")
        )
        row.finished_at = None if creation_failed else now()
    elif row.status not in ("paused", "cancelling") and any(s in ACTIVE for s in statuses):
        row.status = "running"


def list_page(db, query, view=lambda row: row, *, limit=50, offset=0):
    count = db.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
    return {
        "items": [view(row) for row in db.scalars(query.limit(limit).offset(offset))],
        "total": count,
        "limit": limit,
        "offset": offset,
    }


def execution_capabilities():
    from abrl.adapters import adapter_capabilities

    return adapter_capabilities()
